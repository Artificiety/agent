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


def _gather_stop_reason(ar: dict) -> str | None:
    """Why a gather interaction won't run, as a status the caller can act on.

    Shared by the opening interaction and the mid-session resume so the two cannot
    classify the same rejection differently — `depleted` means give up on the node,
    `too_far` means walk back, and anything else names the actual reason instead of
    being retried every tick until the loop times out.
    """
    m = (ar.get("message") or "").lower()
    if "too far" in m:
        return "too_far"
    if "gone" in m:
        return "gone"
    if any(k in m for k in ("deplet", "no interaction", "empty")):
        return "depleted"
    if ar.get("success") is False:
        return (ar.get("reason") or "error").lower()
    return None


def _interrupt(data: dict, stop_on_instruction: bool = True, on_combat: bool = True):
    """The hand-back check every bounded loop here owes its caller, in one place.

    Returns a partial result dict when the snapshot contains a decision point, else
    None. Callers merge their own fields (`gained`, `eaten`, vitals) into it.

    This exists because the checks used to be written out per loop and per observation
    point, and every round of review found another spot that had one and not the other:
    an opening snapshot that skipped straight to the action, a final snapshot that
    reported completion, a branch that returned before reaching them. One helper called
    at every observation point is the only way that stops recurring.

    `on_combat=False` for loops that are *supposed* to be in combat (fight), where
    being engaged is the normal state rather than an interruption.
    """
    if stop_on_instruction:
        pending = pending_instruction_ids(data)
        if pending:
            return {"status": "instruction", "instructionIds": pending}
    if on_combat and is_engaged(data):
        return {"status": "combat", "engaged": data.get("engaged")}
    return None


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

    Stops (status) on: 'arrived', 'combat', 'instruction', 'no_path', 'out_of_range',
    'ended_short', 'low_energy', 'low_satiety', 'target_lost', 'stalled', 'timeout'.

    'arrived' means the destination was actually reached — standing on the tile, or
    adjacent to the entity. When navigation ends anywhere else the status is
    'ended_short' and `tilesAway` says by how much; 'out_of_range' means even a
    single-tile hop was refused, which is terrain in the way rather than distance.

    The result carries `ticks` — how much of the budget this call actually spent — so a
    caller that walks more than once (fight's approach retries) can hold one ceiling
    across the whole operation instead of handing out a fresh one per attempt.
    """
    used = [0]
    res = _travel(client, x, y, entity_id, max_ticks, max_hops, stall_limit,
                  hop_tiles, stop_on_instruction, used)
    res.setdefault("ticks", used[0])
    return res


def _travel(client, x, y, entity_id, max_ticks, max_hops, stall_limit,
            hop_tiles, stop_on_instruction, used):
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
    # Seeded with the tile the FULL-distance request was just refused from, so the first
    # hop is already shorter than it. Starting at None instead makes hop #1 recompute the
    # original destination verbatim and re-ask for the thing that just failed.
    hop_from = last_pos
    hop_span = hop_tiles     # how far a hop reaches; shrinks when it buys no ground
    short_from = None        # tile a re-aim already ended short on

    for tick in range(max_ticks):
        used[0] = tick + 1
        # handle a rejection from the last MOVE_TO issue
        if reason == "NO_PATH":
            return {"status": "no_path", "pos": _pos(data), "message": msg}
        if reason == "LOW_ENERGY":
            return {"status": "low_energy", "pos": _pos(data), "message": msg}
        if reason == "LOW_SATIETY":
            return {"status": "low_satiety", "pos": _pos(data), "message": msg}
        if reason == "OUT_OF_RANGE" and entity_id is None and hops < max_hops:
            # step the destination closer: a hop of ~hop_span toward the target
            cx, cy = _pos(data)
            if cx is None:
                return {"status": "no_path", "pos": (cx, cy), "message": msg}
            dx, dy = int(x) - cx, int(y) - cy
            dist = max(abs(dx), abs(dy)) or 1
            # A hop refused from a tile we never left recomputes the SAME destination and
            # asks again — max_hops identical requests, no movement, budget spent (seen
            # live while routing around a wall). OUT_OF_RANGE is a pathfinding search-COST
            # limit, not a distance limit, so the remedy is a SHORTER hop, not a repeat:
            # halve the span every time it buys no ground, and stop once even one tile is
            # refused, because that is terrain in the way rather than an exhausted budget.
            if (cx, cy) == hop_from:
                hop_span = min(hop_span, dist) // 2
                if hop_span < 1:
                    return {"status": "out_of_range", "pos": (cx, cy), "message": msg}
            else:
                hop_span = hop_tiles  # we gained ground — a full-length hop is worth trying again
            hop_from = (cx, cy)
            step = max(1, min(hop_span, dist))
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

        stop = _interrupt(data, stop_on_instruction)
        if stop:
            return {**stop, "pos": _pos(data)}

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
            # Re-aiming is worth one attempt — the world moves, and a route that ended
            # short can open. Re-aiming from the tile it ALREADY ended short on is not:
            # the destination is blocked or occupied (an NPC parked on it), so repeating
            # burns the whole allowance at a tick apiece to learn the same thing. Hand
            # back the distance and let the caller pick a different tile.
            if hops >= max_hops or cur == short_from:
                return short
            short_from = cur
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
    stop = _interrupt(data, stop_on_instruction)
    if stop:
        return {**stop, "gained": {}}
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

    # Second observation point: the approach may have taken ticks, so re-check before
    # committing to a channeled gather the caller would then have to interrupt.
    opening = client.look()
    stop = _interrupt(opening, stop_on_instruction)
    if stop:
        return {**stop, "gained": {}}
    before = inv_counts(opening)
    r = client.action({"type": "INTERACT", "interaction": interaction, "targetId": node_id})
    ar = r.get("actionResult") or {}
    stop = _gather_stop_reason(ar)
    if stop:
        return {"status": stop, "message": ar.get("message")}

    swings = 0
    for tick in range(max_ticks):
        time.sleep(TICK_SECONDS)
        data = client.look()
        stop = _interrupt(data, stop_on_instruction)
        if stop:
            return {**stop, "gained": _delta(before, inv_counts(data))}
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
            rar = rr.get("actionResult") or {}
            status = _gather_stop_reason(rar)
            if status:
                return {"status": status, "gained": _delta(before, inv_counts(client.look())),
                        "message": rar.get("message")}
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
    # Before `reached`: an instruction sitting in this first response would otherwise be
    # dropped entirely, because the loop's check never runs when the target is already met.
    stop = _interrupt(data, stop_on_instruction)
    if stop:
        return {**stop, "energy": data.get("energy"), "health": data.get("health")}
    # already there — don't burn 80 ticks confirming it
    if hit(data):
        return reached(data)

    for tick in range(max_ticks):
        time.sleep(TICK_SECONDS)
        data = client.look()
        stop = _interrupt(data, stop_on_instruction)
        if stop:
            return {**stop, "energy": data.get("energy"), "health": data.get("health")}
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
    # An instruction already waiting outranks starting a fight: combat then resolves on
    # its own, so striking first commits the agent before handing control back.
    stop = _interrupt(data, stop_on_instruction, on_combat=False)
    if stop:
        return {**stop, "health": data.get("health")}

    # ONE tick budget for the whole helper. Each approach retry used to get a fresh
    # `max_ticks`, and the watch loop another on top, so the advertised 40-tick ceiling
    # could stretch to ~240 across five retries.
    budget = [max_ticks]

    def spend(used):
        budget[0] = max(0, budget[0] - max(0, used))
        return budget[0]

    tries = 0
    while not is_engaged(data):
        ent = _entity(data, target_id)
        if ent is None:
            return {"status": "gone"}
        if approach and (ent.get("distance") or 99) > 1:
            if budget[0] <= 0:
                return {"status": "timeout", "health": data.get("health"),
                        "message": "tick budget spent approaching"}
            tr = travel_to(client, entity_id=target_id, max_ticks=budget[0],
                           stop_on_instruction=stop_on_instruction)
            spend(tr.get("ticks", budget[0]))
            if tr["status"] == "combat":  # something engaged us; whose fight it is
                break                     # gets settled at the one check below
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

    # Whose fight is this? Every way into the watch loop passes here — already engaged
    # on entry, engaged mid-walk, engaged during the post-arrival refresh, or our own
    # opening swing — so the target check belongs at this one chokepoint. Checking it
    # per break site is what let three separate paths each miss it in turn.
    engaged_with = (data.get("engaged") or {}).get("targetId")
    if engaged_with and engaged_with != target_id:
        return {"status": "new_threat", "engaged": data.get("engaged"),
                "health": data.get("health"),
                "gained": _delta(start_inv, _inv_counts(data)),
                "message": f"engaged with a different creature ({engaged_with}), not {target_id}"}

    for tick in range(budget[0]):
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

        stop = _interrupt(data, stop_on_instruction, on_combat=False)
        if stop:
            return {**stop, "health": data.get("health"),
                    "gained": _delta(start_inv, _inv_counts(data))}

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


def eat(client, item_id, until_pct=None, max_count=10, stop_on_instruction=True):
    """Mechanically CONSUME `item_id` (until a hunger percent, or a count).

    WHEN to eat is the player's call; this only does the eating. Stops (status) on:
    'fed', 'out_of_food', 'not_consumable', 'count', 'already_full', 'combat',
    'instruction'.
    """
    eaten = 0
    for _ in range(max_count):
        data = client.look()
        # Same hand-back contract as every other loop here: an owner instruction or a
        # fight is a decision point, and eating ten items through one takes ~30s.
        stop = _interrupt(data, stop_on_instruction)
        if stop:
            return {**stop, "eaten": eaten, "hunger": data.get("hunger")}
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

    # The count ran out, but this last look may still be carrying the decision point
    # that arrived during the final sleep. Reporting only 'count' would hide it.
    data = client.look()
    stop = _interrupt(data, stop_on_instruction)
    if stop:
        return {**stop, "eaten": eaten, "hunger": data.get("hunger")}
    return {"status": "count", "eaten": eaten, "hunger": data.get("hunger")}
