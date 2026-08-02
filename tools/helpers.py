"""Bounded, interruptible mechanical loops — the tedious multi-tick parts, done once.

Every function here executes ONE concrete mechanical intent (walk a route, work a
node, rest to a number, watch a fight) and then STOPS, handing a compact result
back to the caller. They are strictly:

  - bounded: a hard `max_ticks` ceiling, never open-ended,
  - interruptible: they return the moment something needs a decision — a pending
    owner instruction, combat starting, a vital threshold, a blocked path,
  - dumb: they encode no strategy. WHERE to go, WHETHER a fight is worth it, WHEN
    to eat — that judgement stays with the player, tick by tick.

There is deliberately no `farm_gold()` / `get_rich()` here: goals in an emergent,
real-time world can't be scripted, only decided live.

Each returns a dict with at least `status` (a stop-reason) so the caller knows why
control came back.
"""
from __future__ import annotations

import sys
import time

TICK_SECONDS = 3.0

_GATHER_INTERACTIONS = ("CHOP", "MINE", "FORAGE", "FISH")


def _progress(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def pending_instruction_ids(data: dict) -> list[str]:
    return [i.get("id") for i in (data.get("instructions") or []) if i.get("id")]


def is_engaged(data: dict) -> bool:
    return bool(data.get("engaged"))


def _pos(data: dict):
    sur = data.get("surroundings") or {}
    return sur.get("x"), sur.get("y")


def _entity(data: dict, entity_id: str):
    for e in ((data.get("surroundings") or {}).get("nearbyEntities") or []):
        if e.get("id") == entity_id:
            return e
    return None


_CARDINALS = (("north", 0, -1), ("south", 0, 1), ("east", 1, 0), ("west", -1, 0))


def _walkable_dirs(data: dict) -> list[str]:
    """Cardinal directions from the current tile that resolve to walkable neighbours."""
    sur = data.get("surroundings") or {}
    x, y = sur.get("x"), sur.get("y")
    terr = {(t.get("x"), t.get("y")): t.get("walkable") for t in (sur.get("nearbyTerrain") or [])}
    out = []
    for name, dx, dy in _CARDINALS:
        # default True when the tile is out of the terrain sample — better to try than to freeze
        if terr.get((x + dx, y + dy), True):
            out.append(name)
    return out


def _below_flee(data: dict, flee_hp) -> bool:
    """True when HP is at or under the caller's hand-back threshold (percent or absolute)."""
    if flee_hp is None:
        return False
    val = (_vitals_pct(data, "health") if flee_hp <= 100
           else (data.get("health") or {}).get("current", 9999))
    return val <= flee_hp


def _vitals_pct(data: dict, which: str) -> int:
    v = data.get(which) or {}
    key = {"energy": "energy", "hunger": "hunger", "health": "current"}[which]
    mxk = {"energy": "maxEnergy", "hunger": "maxHunger", "health": "max"}[which]
    cur, mx = v.get(key), v.get(mxk)
    try:
        return int(round(100 * cur / mx))
    except Exception:
        return 0


def travel_to(client, x=None, y=None, entity_id=None, max_ticks=45,
              max_hops=6, stall_limit=4, hop_tiles=18, stop_on_instruction=True):
    """Walk toward a coordinate or entity, re-pathing in short hops.

    Stops (status) on: 'arrived', 'combat', 'instruction', 'no_path',
    'low_energy', 'low_satiety', 'target_lost', 'stalled', 'timeout'.
    """
    if entity_id:
        target = {"type": "MOVE_TO", "targetId": entity_id}
    elif x is not None and y is not None:
        target = {"type": "MOVE_TO", "x": int(x), "y": int(y)}
    else:
        return {"status": "error", "reason": "need x/y or entity_id"}

    def issue(payload):
        d = client.action(payload)
        ar = d.get("actionResult") or {}
        return d, ar.get("reason"), ar.get("message", "")

    data, reason, msg = issue(target)
    hops = 0
    last_pos = _pos(data)
    stall = 0

    for tick in range(max_ticks):
        # handle a rejection from the last MOVE_TO issue
        if reason == "NO_PATH":
            return {"status": "no_path", "pos": _pos(data), "message": msg}
        if reason == "LOW_ENERGY":
            return {"status": "low_energy", "pos": _pos(data), "message": msg}
        if reason == "LOW_SATIETY":
            return {"status": "low_satiety", "pos": _pos(data), "message": msg}
        if reason == "OUT_OF_RANGE" and entity_id is None and hops < max_hops:
            # step the destination closer: a hop of ~hop_tiles toward the target
            cx, cy = _pos(data)
            if cx is None:
                return {"status": "no_path", "pos": (cx, cy), "message": msg}
            dx, dy = int(x) - cx, int(y) - cy
            dist = max(abs(dx), abs(dy)) or 1
            step = min(hop_tiles, dist)
            hx = cx + round(dx * step / dist)
            hy = cy + round(dy * step / dist)
            hops += 1
            _progress(f"  travel: OUT_OF_RANGE, hopping to ({hx},{hy}) [{hops}/{max_hops}]")
            data, reason, msg = issue({"type": "MOVE_TO", "x": hx, "y": hy})
            continue
        if reason == "OUT_OF_RANGE":
            return {"status": "out_of_range", "pos": _pos(data), "message": msg}

        time.sleep(TICK_SECONDS)
        data = client.look()

        if stop_on_instruction and pending_instruction_ids(data):
            return {"status": "instruction", "pos": _pos(data),
                    "instructionIds": pending_instruction_ids(data)}
        if is_engaged(data):
            return {"status": "combat", "pos": _pos(data),
                    "engaged": data.get("engaged")}

        for ev in (data.get("events") or []):
            if ev.get("type") == "waypoint.target_lost":
                return {"status": "target_lost", "pos": _pos(data)}

        cur = _pos(data)
        wp = data.get("waypoint")
        if wp is None:
            # Navigation ENDED — which is not the same as arrived. The server also
            # drops the waypoint when a route is cancelled or blocked, so confirm the
            # destination was actually reached before reporting success: for an entity
            # target that means adjacent, for coords it means standing on the tile.
            if entity_id is None:
                if cur == (int(x), int(y)):
                    return {"status": "arrived", "pos": cur}
                short = {"status": "ended_short", "pos": cur,
                         "tilesAway": max(abs(cur[0] - int(x)), abs(cur[1] - int(y)))
                         if cur[0] is not None else None}
            else:
                node = _entity(data, entity_id)
                if node is None:
                    return {"status": "target_lost", "pos": cur}
                if node.get("distance", 99) <= 1:
                    return {"status": "arrived", "pos": cur}
                short = {"status": "ended_short", "pos": cur,
                         "tilesAway": node.get("distance")}
            if hops >= max_hops:
                return short
            hops += 1
            _progress(f"  travel: route ended {short['tilesAway']} tiles short, re-aiming [{hops}/{max_hops}]")
            data, reason, msg = issue(target if entity_id else
                                      {"type": "MOVE_TO", "x": int(x), "y": int(y)})
            continue

        stall = stall + 1 if cur == last_pos else 0
        last_pos = cur
        if stall >= stall_limit:
            return {"status": "stalled", "pos": cur,
                    "tilesRemaining": (wp or {}).get("tilesRemaining")}
        reason = None  # only the initial issue carried a reason

    return {"status": "timeout", "pos": _pos(data)}


def gather(client, node_id, interaction=None, until=None, approach=True,
           max_ticks=60, stop_on_instruction=True):
    """Work a resource node until it's dry (or `until` swings land / inventory fills).

    `interaction` (CHOP/MINE/FORAGE/FISH) is auto-detected from the node when omitted.
    Stops (status) on: 'depleted', 'reached', 'inventory_full', 'combat',
    'instruction', 'too_far', 'gone', 'timeout'. Reports `gained` (inventory delta).
    """
    data = client.look()
    node = _entity(data, node_id)
    if node is None:
        return {"status": "gone"}
    if interaction is None:
        avail = [i for i in (node.get("interactions") or []) if i in _GATHER_INTERACTIONS]
        if not avail:
            return {"status": "error", "reason": f"node has no gather interaction: {node.get('interactions')}"}
        interaction = avail[0]

    if approach and (node.get("distance") or 99) > 1:
        tr = travel_to(client, entity_id=node_id, stop_on_instruction=stop_on_instruction)
        if tr["status"] != "arrived":
            return {"status": "approach_" + tr["status"], "travel": tr}

    def inv_counts(d):
        return {it.get("itemId"): it.get("quantity", 0)
                for it in ((d.get("inventory") or {}).get("items") or [])}

    before = inv_counts(client.look())
    r = client.action({"type": "INTERACT", "interaction": interaction, "targetId": node_id})
    ar = r.get("actionResult") or {}
    if ar.get("success") is False:
        m = (ar.get("message") or "").lower()
        if "too far" in m:
            return {"status": "too_far", "message": ar.get("message")}
        return {"status": "error", "message": ar.get("message")}

    swings = 0
    for tick in range(max_ticks):
        time.sleep(TICK_SECONDS)
        data = client.look()
        if stop_on_instruction and pending_instruction_ids(data):
            return {"status": "instruction", "gained": _delta(before, inv_counts(data)),
                    "instructionIds": pending_instruction_ids(data)}
        if is_engaged(data):
            return {"status": "combat", "gained": _delta(before, inv_counts(data)),
                    "engaged": data.get("engaged")}
        for ev in (data.get("events") or []):
            if ev.get("type") == "resource.gathered":
                swings += 1
        inv = data.get("inventory") or {}
        if (inv.get("usedSlots") or 0) >= (inv.get("maxSlots") or 1) and not data.get("currentActivity"):
            return {"status": "inventory_full", "gained": _delta(before, inv_counts(data))}
        if until is not None and swings >= until:
            return {"status": "reached", "gained": _delta(before, inv_counts(data)), "swings": swings}
        if not data.get("currentActivity"):
            # session ended — try to resume; if it won't, the node is dry/too far
            rr = client.action({"type": "INTERACT", "interaction": interaction, "targetId": node_id})
            msg = (rr.get("actionResult") or {}).get("message")
            m = (msg or "").lower()
            # Distinguish why it won't resume: a node we drifted away from is still
            # worth walking back to, whereas 'depleted' tells the caller to give up on it.
            if "too far" in m:
                status = "too_far"
            elif "gone" in m:
                status = "gone"
            elif any(k in m for k in ("deplet", "no interaction", "empty")):
                status = "depleted"
            else:
                status = None
            if status:
                return {"status": status, "gained": _delta(before, inv_counts(client.look())),
                        "message": msg}
    return {"status": "timeout", "gained": _delta(before, inv_counts(client.look())), "swings": swings}


def _inv_counts(data):
    return {it.get("itemId"): it.get("quantity", 0)
            for it in ((data.get("inventory") or {}).get("items") or [])}


def _delta(before, after):
    out = {}
    for k, v in after.items():
        d = v - before.get(k, 0)
        if d > 0:
            out[k] = d
    return out


def rest_until(client, energy=None, health=None, max_ticks=80, stop_on_instruction=True):
    """REST until an energy/health target (percent or absolute) is reached.

    Targets given as <=100 are treated as percent, else absolute. Stops (status)
    on: 'reached', 'full', 'combat', 'instruction', 'timeout'.

    Combat ends the wait immediately: resting stops the moment something engages the
    agent, and the caller has to decide whether to fight or run — re-casting REST into
    an active fight would hold control here while auto-combat plays out.

    On 'reached' the server-side REST may still be running. There is no stop-resting
    action (any non-LOOK action the caller sends next cancels it), so the result
    reports `resting` rather than implying it was stopped.
    """
    def hit(data):
        ok = True
        if energy is not None:
            val = _vitals_pct(data, "energy") if energy <= 100 else (data.get("energy") or {}).get("energy", 0)
            ok = ok and val >= energy
        if health is not None:
            val = _vitals_pct(data, "health") if health <= 100 else (data.get("health") or {}).get("current", 0)
            ok = ok and val >= health
        return ok

    def reached(data):
        return {"status": "reached", "energy": data.get("energy"), "health": data.get("health"),
                "resting": bool((data.get("energy") or {}).get("resting"))}

    data = client.action({"type": "INTERACT", "interaction": "REST"})
    ar = data.get("actionResult") or {}
    if ar.get("success") is False and ar.get("reason") == "COMBAT_STILL_ACTIVE":
        return {"status": "combat", "message": ar.get("message")}
    if is_engaged(data):
        return {"status": "combat", "engaged": data.get("engaged"), "health": data.get("health")}
    # already there — don't burn 80 ticks confirming it
    if hit(data):
        return reached(data)

    for tick in range(max_ticks):
        time.sleep(TICK_SECONDS)
        data = client.look()
        if stop_on_instruction and pending_instruction_ids(data):
            return {"status": "instruction", "energy": data.get("energy"),
                    "instructionIds": pending_instruction_ids(data)}
        if is_engaged(data):
            return {"status": "combat", "engaged": data.get("engaged"),
                    "energy": data.get("energy"), "health": data.get("health")}
        if hit(data):
            return reached(data)
        if not (data.get("energy") or {}).get("resting"):
            # rest auto-stopped (probably full) — re-cast unless we've hit the cap
            en = data.get("energy") or {}
            if (en.get("energy") or 0) >= (en.get("maxEnergy") or 1):
                return {"status": "full", "energy": en}
            client.action({"type": "INTERACT", "interaction": "REST"})
    return {"status": "timeout", "energy": data.get("energy")}


def fight(client, target_id, flee_hp=None, approach=True, approach_tries=4,
          max_ticks=40, stop_on_instruction=True):
    """Approach (if needed), start, and WATCH an automatic fight, handing back fast.

    Combat resolves on its own; this only watches and returns on a decision point.
    Roaming targets drift out of attack range between the walk finishing and the
    strike landing, so the approach+strike is retried up to `approach_tries` times.
    Stops (status) on: 'killed' (+loot), 'low_hp' (HP<=flee_hp, does NOT auto-flee —
    the caller decides), 'new_threat' (a 2nd aggressive creature closed in),
    'combat_ended' (target gone/fled), 'gone' (target left view), 'instruction',
    'timeout'.
    """
    data = client.look()
    start_inv = _inv_counts(data)  # to report what dropped, even when the label is combat_ended
    # Already at the hand-back threshold: opening a fight here is the exact state
    # `flee_hp` exists to avoid, and the opening swing invites a hit back before the
    # watch loop below would ever get to check.
    if _below_flee(data, flee_hp):
        return {"status": "low_hp", "health": data.get("health"), "engaged": data.get("engaged")}
    # Already fighting something else when called: the approach loop below is skipped
    # entirely, so its target check never runs and we would watch the wrong fight for
    # up to max_ticks. Same substitution as the mid-approach case, different entry.
    eng_at_entry = (data.get("engaged") or {}).get("targetId")
    if eng_at_entry and eng_at_entry != target_id:
        return {"status": "new_threat", "engaged": data.get("engaged"),
                "health": data.get("health"),
                "message": "already engaged with a different creature"}
    tries = 0
    while not is_engaged(data):
        ent = _entity(data, target_id)
        if ent is None:
            return {"status": "gone"}
        if approach and (ent.get("distance") or 99) > 1:
            tr = travel_to(client, entity_id=target_id, max_ticks=max_ticks,
                           stop_on_instruction=stop_on_instruction)
            if tr["status"] == "combat":
                # Something engaged us mid-walk. If it is the target we were asked to
                # fight, that is a fine way to start. If it is a different creature,
                # watching that fight would silently substitute the caller's decision.
                eng = (tr.get("engaged") or {}).get("targetId")
                if eng and eng != target_id:
                    return {"status": "new_threat", "engaged": tr.get("engaged"),
                            "health": tr.get("health"),
                            "message": "engaged by a different creature while approaching"}
                break
            if tr["status"] == "instruction":
                return {"status": "instruction", "instructionIds": tr.get("instructionIds")}
            if tr["status"] != "arrived":
                return {"status": "approach_" + tr["status"], "travel": tr}
            data = client.look()
            if is_engaged(data):
                break
        r = client.action({"type": "INTERACT", "interaction": "ATTACK", "targetId": target_id})
        ar = r.get("actionResult") or {}
        if ar.get("success") is not False:
            data = r  # keep the opening swing's events (a one-shot kill fires here, not in a later LOOK)
            break
        m = (ar.get("message") or "").lower()
        tries += 1
        if tries > approach_tries:
            return {"status": "too_far" if "too far" in m else "error",
                    "message": ar.get("message")}
        if "overlap" in m or "standing on" in m:
            # entity nav landed us ON the target's tile — step to a neighbour, then retry
            data = client.look()
            dirs = _walkable_dirs(data)
            if dirs:
                client.action({"type": "MOVE", "direction": dirs[0]})
            data = client.look()
            continue
        if "too far" in m:
            data = client.look()  # target drifted — re-approach and retry
            continue
        return {"status": "error", "message": ar.get("message")}

    # a one-shot opening kill fires in the ATTACK response we kept as `data`; catch it
    # here, otherwise refresh so the watch loop never runs on stale pre-approach state
    # (an aggro target that engaged us mid-walk left `data` stale).
    killed_ev = next((ev for ev in (data.get("events") or [])
                      if ev.get("type") == "creature.killed"), None)
    if killed_ev:
        return {"status": "killed", "loot": killed_ev.get("message"),
                "gained": _delta(start_inv, _inv_counts(data)), "health": data.get("health")}
    data = client.look()

    for tick in range(max_ticks):
        for ev in (data.get("events") or []):
            t = ev.get("type") or ""
            if t == "creature.killed":
                return {"status": "killed", "loot": ev.get("message"),
                        "gained": _delta(start_inv, _inv_counts(data)),
                        "health": data.get("health")}
            if t == "combat.ended":
                return {"status": "combat_ended",
                        "gained": _delta(start_inv, _inv_counts(data)),
                        "health": data.get("health")}

        if stop_on_instruction and pending_instruction_ids(data):
            return {"status": "instruction", "health": data.get("health"),
                    "instructionIds": pending_instruction_ids(data)}

        if _below_flee(data, flee_hp):
            return {"status": "low_hp", "health": data.get("health"), "engaged": data.get("engaged")}

        # a second aggressive creature adjacent-ish while we're mid-fight = decision point
        eng_id = (data.get("engaged") or {}).get("targetId")
        others = [e for e in ((data.get("surroundings") or {}).get("nearbyEntities") or [])
                  if e.get("type") == "CREATURE" and (e.get("creatureInfo") or {}).get("aggressive")
                  and e.get("id") != eng_id and (e.get("distance") or 99) <= 2]
        if others:
            return {"status": "new_threat", "health": data.get("health"),
                    "threats": [f"{o.get('name')}(d{o.get('distance')})" for o in others]}

        if not is_engaged(data):
            return {"status": "combat_ended",
                    "gained": _delta(start_inv, _inv_counts(data)),
                    "health": data.get("health")}

        time.sleep(TICK_SECONDS)
        data = client.look()

    return {"status": "timeout", "health": data.get("health"), "engaged": data.get("engaged")}


def eat(client, item_id, until_pct=None, max_count=10):
    """Mechanically CONSUME `item_id` (until a hunger percent, or a count).

    WHEN to eat is the player's call; this only does the eating. Stops (status) on:
    'fed', 'out_of_food', 'not_consumable', 'count', 'already_full'.
    """
    eaten = 0
    for _ in range(max_count):
        data = client.look()
        have = next((it for it in ((data.get("inventory") or {}).get("items") or [])
                     if it.get("itemId") == item_id), None)
        if not have:
            return {"status": "out_of_food" if eaten == 0 else "fed", "eaten": eaten,
                    "hunger": data.get("hunger")}
        if until_pct is not None and _vitals_pct(data, "hunger") >= until_pct:
            return {"status": "fed", "eaten": eaten, "hunger": data.get("hunger")}
        r = client.action({"type": "INTERACT", "interaction": "CONSUME", "itemId": item_id})
        ar = r.get("actionResult") or {}
        if ar.get("success") is False:
            reason = ar.get("reason")
            if reason == "ALREADY_SATISFIED":
                return {"status": "already_full", "eaten": eaten, "hunger": r.get("hunger")}
            if reason == "NOT_CONSUMABLE":
                return {"status": "not_consumable", "message": ar.get("message")}
            return {"status": "error", "message": ar.get("message")}
        eaten += 1
        time.sleep(TICK_SECONDS)
    return {"status": "count", "eaten": eaten, "hunger": client.look().get("hunger")}
