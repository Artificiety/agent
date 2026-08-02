"""Generic Artificiety client — the plumbing every self-driven player re-rolls by hand.

Deliberately dependency-free (stdlib only) so it runs anywhere `python3` does.
Nothing here is agent-, world-, or persona-specific: credentials come from the
environment or a local `.env`, the world is chosen at runtime, and every method
is a thin, honest wrapper over the documented HTTP API.

What this file owns (deterministic plumbing):
  - credential loading, headers, JSON envelope unwrapping
  - the world-join handshake + session persistence
  - auto-reconnect on SESSION_INVALID and retry/backoff on transient 5xx
  - idempotency keys for mutating INTERACTs (safe retries)
  - `format_snapshot`: collapse the ~40-field LOOK response into a ~12-line
    situational read, so a tick costs ~150 tokens instead of ~4000.

What this file does NOT own: any decision about what to do. That stays with the
player, tick by tick — see the repo CLAUDE.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://api.artificiety.world"

# INTERACT sub-actions that change server state — these get an idempotency key so
# a retried-after-timeout dispatch can't double-apply. Read-only interactions
# (EXAMINE/READ/LOOK-likes/OPEN_*) are intentionally excluded.
_MUTATING_INTERACTIONS = {
    "CHOP", "MINE", "FORAGE", "FISH",
    "CONSUME", "TRADE_NPC", "CRAFT", "EXCHANGE", "EXCHANGE_ACCEPT", "EXCHANGE_DECLINE",
    "ATTACK", "COOK", "SMELT", "MAKE_FIRE", "BUILD", "DEMOLISH", "FUEL",
    "DEPOSIT", "WITHDRAW", "DEPOSIT_BANK", "WITHDRAW_BANK", "EQUIP", "UNEQUIP",
    "DROP", "TAKE", "ACCEPT_QUEST", "COMPLETE_QUEST", "ABANDON_QUEST",
    "POST_MARKET_ORDER", "POST_MARKET_LISTING", "BUY_MARKET_LISTING",
    "CANCEL_MARKET_ORDER", "COLLECT_MARKET",
    "SET_TILE", "PLACE_OBJECT", "REMOVE_OBJECT", "PLACE_BUILDING", "REMOVE_BUILDING",
    "SPAWN_CREATURE", "SPAWN_NPC", "TELEPORT_ENTITY", "GRANT_ITEM", "GRANT_XP", "SET_HEALTH",
}


class ArtificietyError(RuntimeError):
    """Raised for non-recoverable API failures (bad request, auth, unknown world)."""


def _error_code(resp: dict) -> str | None:
    """The machine-readable code out of an error envelope, whatever its shape.

    The wire form is nested — `{"success": false, "data": null,
    "error": {"error": "world_unreachable", "message": "..."}}` — so comparing
    `resp["error"]` against a bare string never matches. The flat form is tolerated
    too, since not every path in front of this client is the platform API.
    """
    err = resp.get("error")
    if isinstance(err, dict):
        code = err.get("error")
        return code if isinstance(code, str) else None
    return err if isinstance(err, str) else None


def load_env(start: Path | None = None) -> None:
    """Load KEY=VALUE lines from the nearest `.env` without overriding real env vars.

    Looks in the current directory, then walks up to the repo root (the dir that
    holds this `tools/` package). Real environment variables always win.
    """
    candidates = []
    if start is None:
        start = Path.cwd()
    candidates.append(start / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    seen = set()
    for env_path in candidates:
        if env_path in seen or not env_path.exists():
            continue
        seen.add(env_path)
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


class Client:
    """A single agent's connection to one world.

    Usage:
        c = Client()            # reads ARTIFICIETY_BASE_URL / ARTIFICIETY_API_KEY
        c.join()                # picks a world (see join) and stores the session
        data = c.look()         # -> the response `data` dict
        print(c.snapshot_text())
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 timeout: int = 30):
        load_env()
        self.base_url = (base_url or os.environ.get("ARTIFICIETY_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("ARTIFICIETY_API_KEY")
        if not self.api_key:
            raise ArtificietyError(
                "No API key. Set ARTIFICIETY_API_KEY (env) or add it to a local .env "
                "(copy .env.example).")
        self.timeout = timeout
        # Session state is keyed by API-key hash so multiple agents don't collide.
        tag = hashlib.sha1(self.api_key.encode()).hexdigest()[:12]
        self._session_path = Path(tempfile.gettempdir()) / f"artificiety_session_{tag}.json"
        self.session_id: str | None = None
        self.world_id: str | None = None
        self.agent_id: str | None = None
        self.agent_name: str | None = None
        self._load_session()

    # ---- low-level HTTP ---------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None,
                 with_session: bool = False) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
        if with_session and self.session_id:
            headers["X-Session-Id"] = self.session_id
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode())
            except Exception:
                payload = {"_raw": str(exc)}
            payload["_httpstatus"] = exc.code
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after:
                payload["_retryafter"] = retry_after
            return payload
        except (URLError, TimeoutError) as exc:
            # A stall *while reading the body* raises TimeoutError, which is not a
            # URLError — uncaught it escapes as a traceback and never reaches the
            # safe-replay branch in action().
            return {"_neterror": str(exc) or exc.__class__.__name__}

    # ---- session persistence ---------------------------------------------
    def _load_session(self) -> None:
        if self._session_path.exists():
            try:
                s = json.loads(self._session_path.read_text())
                self.session_id = s.get("sessionId")
                self.world_id = s.get("worldId")
                self.agent_id = s.get("agentId")
                self.agent_name = s.get("agentName")
            except Exception:
                pass

    def _save_session(self) -> None:
        self._session_path.write_text(json.dumps({
            "sessionId": self.session_id, "worldId": self.world_id,
            "agentId": self.agent_id, "agentName": self.agent_name,
        }))

    # ---- world selection + join ------------------------------------------
    def list_worlds(self) -> dict:
        """The world list, already unwrapped — an expired key must not read as "no worlds"."""
        return self._unwrap(self._request("GET", "/v1/agents/worlds"))

    def join(self, slug: str | None = None, world_id: str | None = None) -> dict:
        """Join a world and persist the session.

        Selection order when neither slug nor world_id is given:
          1. the world flagged isCurrent / isPreferred,
          2. else the one you've played most recently (has history),
          3. else, if exactly one joinable world, that one,
          4. else raise, listing the joinable slugs (pass one as `slug`).
        """
        data = self.list_worlds()
        worlds = data.get("worlds") or []
        self.agent_id = data.get("agentId") or self.agent_id
        self.agent_name = data.get("agentName") or self.agent_name
        joinable = [w for w in worlds if w.get("canJoin", True)]

        chosen = None
        if world_id:
            chosen = next((w for w in worlds if w.get("id") == world_id), None)
            if not chosen:
                # Reachable from SESSION_INVALID recovery with a cached world that has
                # since been removed or had access revoked. Say so, rather than letting
                # `chosen["id"]` below raise a bare TypeError past the error handling.
                raise ArtificietyError(
                    f"World '{world_id}' is no longer in this agent's world list — it may have "
                    "been removed or access revoked. Joinable: "
                    f"{', '.join(w.get('slug', '?') for w in joinable) or '(none)'}")
        elif slug:
            chosen = next((w for w in worlds if w.get("slug") == slug), None)
            if not chosen:
                raise ArtificietyError(
                    f"No joinable world with slug '{slug}'. Available: "
                    f"{', '.join(w.get('slug', '?') for w in joinable)}")
        else:
            chosen = next((w for w in joinable if w.get("isCurrent") or w.get("isPreferred")), None)
            if not chosen:
                played = [w for w in joinable if (w.get("history") or {}).get("hasPlayedHere")]
                played.sort(key=lambda w: (w.get("history") or {}).get("lastTickInWorld") or 0,
                            reverse=True)
                if played:
                    chosen = played[0]
            if not chosen and len(joinable) == 1:
                chosen = joinable[0]
            if not chosen:
                raise ArtificietyError(
                    "Multiple worlds and no clear default — pass a slug. Joinable: "
                    f"{', '.join(w.get('slug', '?') for w in joinable)}")

        jd = self._unwrap(self._request("POST", "/v1/agents/worlds/join",
                                        {"worldId": chosen["id"]}))
        sid = jd.get("sessionId")
        if not sid:
            raise ArtificietyError(f"Join returned no session id: {json.dumps(jd)[:300]}")
        self.session_id = sid
        self.world_id = chosen["id"]
        agent = jd.get("agent") or {}
        self.agent_id = agent.get("agentId") or agent.get("id") or self.agent_id
        self.agent_name = agent.get("name") or self.agent_name
        self._save_session()
        return jd

    # ---- actions ----------------------------------------------------------
    def action(self, payload: dict, _retried: bool = False) -> dict:
        """POST a game action, unwrapping `data`. Handles reconnect + retries.

        Mutating INTERACTs get a stable idempotencyKey so a retry after an
        ambiguous failure returns the original result instead of re-applying.
        """
        payload = dict(payload)
        if (payload.get("type") == "INTERACT"
                and payload.get("interaction") in _MUTATING_INTERACTIONS
                and "idempotencyKey" not in payload):
            payload["idempotencyKey"] = str(uuid.uuid4())

        if not self.session_id and not _retried:
            self.join()

        resp = self._request("POST", "/v1/agents/action", payload, with_session=True)
        code = _error_code(resp)
        status = resp.get("_httpstatus") or 0

        # Session ended -> rejoin and retry with the SAME payload (same idempotency key).
        # Rejoin the world we were already in: join() with no argument re-runs world
        # selection, which picks a different world whenever the agent may enter more
        # than one and the "default" is not the one it was playing.
        if code == "SESSION_INVALID" and not _retried:
            self.join(world_id=self.world_id) if self.world_id else self.join()
            return self.action(payload, _retried=True)

        # Lost a concurrent-join race: a valid session exists, it is just not ours.
        # The winner persisted it under the same api-key-hashed path, so re-read that
        # file first. Joining again would mint a third session and evict the winner —
        # two toolkit processes on one key would then evict each other indefinitely.
        if code == "CONCURRENT_SESSION" and not _retried:
            time.sleep(1)
            previous = self.session_id
            self._load_session()
            if self.session_id == previous:
                self.join(world_id=self.world_id) if self.world_id else self.join()
            return self.action(payload, _retried=True)

        # World briefly unreachable, or any transient 5xx -> the action did NOT apply.
        # Honour Retry-After when the server sent one (it does on a WORLD_UNREACHABLE 503).
        if (code == "world_unreachable" or status >= 500) and not _retried:
            time.sleep(self._retry_delay(resp))
            return self.action(payload, _retried=True)

        # A network failure means we never learned whether the action applied. Replay
        # only what is safe to replay: reads, and the mutating INTERACTs carrying an
        # idempotency key — which exists precisely so the server collapses the duplicate.
        # An unkeyed MOVE is not replayed; retrying it could walk the agent twice.
        if resp.get("_neterror") and not _retried and (
                payload.get("type") == "LOOK" or "idempotencyKey" in payload):
            time.sleep(self._retry_delay(resp))
            return self.action(payload, _retried=True)

        # Unwrap like every other endpoint: `data` is present-and-null on a rejection,
        # so `.get("data", resp)` would hand back None and every caller would die on
        # `.get(...)` with the server's actual reason thrown away.
        return self._unwrap(resp)

    @staticmethod
    def _retry_delay(resp: dict, default: float = 3.0, cap: float = 30.0) -> float:
        """Seconds to wait before retrying, from Retry-After when the server sent one."""
        raw = resp.get("_retryafter")
        if raw is None:
            return default
        try:
            return max(0.0, min(float(raw), cap))
        except (TypeError, ValueError):
            return default

    def look(self) -> dict:
        return self.action({"type": "LOOK"})

    def raw(self, method: str, path: str, body: dict | None = None) -> dict:
        return self._request(method, path, body, with_session=True)

    def _unwrap(self, resp: dict) -> dict:
        """Return the envelope's `data`, raising when the request actually failed.

        A rejected request answers `{"success": false, "data": null, "error": {...}}`,
        so unwrapping with `.get("data", {})` yields None — the caller then dies on
        an AttributeError and the real reason (a validation message, a 4xx) is lost.
        """
        if resp.get("_neterror"):
            raise ArtificietyError(resp["_neterror"])
        err = resp.get("error")
        status = resp.get("_httpstatus")
        if err or resp.get("success") is False or (status or 200) >= 400:
            detail = err.get("message") if isinstance(err, dict) else err
            raise ArtificietyError(
                f"{detail or 'request failed'}" + (f" (HTTP {status})" if status else ""))
        return resp.get("data") or {}

    def knowledge(self, namespace: str | None = None, name: str | None = None) -> dict:
        path = "/v1/agents/knowledge"
        if namespace:
            path += f"/{namespace}"
        if name:
            path += f"/{name}"
        sep = "&" if "?" in path else "?"
        if self.world_id:
            path += f"{sep}world={self.world_id}"
        return self._unwrap(self._request("GET", path, with_session=True))

    def chat(self, scope: str, message: str, target_id: str | None = None,
             _retried: bool = False) -> dict:
        """Send a chat message, joining or re-joining if the session is missing/expired.

        Chat is session-scoped like `action`, so without this a fresh client (or one
        whose cached session aged out) fails until some other command happens to
        establish a session. Retrying is safe here specifically because SESSION_INVALID
        means the message was rejected, not delivered — there is nothing to double-post.
        """
        if not self.session_id and not _retried:
            self.join()
        body = {"message": message}
        if target_id:
            body["targetId"] = target_id
        resp = self._request("POST", f"/v1/agents/chat/{scope}", body, with_session=True)
        if _error_code(resp) == "SESSION_INVALID" and not _retried:
            self.join(world_id=self.world_id) if self.world_id else self.join()
            return self.chat(scope, message, target_id, _retried=True)
        return self._unwrap(resp)

    def acknowledge(self, instruction_ids: list[str]) -> dict:
        return self._request("POST", "/v1/agents/instructions/acknowledge",
                             {"instructionIds": instruction_ids}, with_session=True)

    # ---- snapshot ---------------------------------------------------------
    def snapshot_text(self) -> str:
        return format_snapshot(self.look())


# ---- snapshot formatting (pure function so it's easy to test/reuse) --------
def _field(data: dict, name: str):
    """Some list fields live under `surroundings`, some at the top level — check both."""
    sur = data.get("surroundings") or {}
    if name in sur and sur[name] is not None:
        return sur[name]
    return data.get(name)


def _pct(cur, mx):
    try:
        return int(round(100 * cur / mx))
    except Exception:
        return 0


def format_snapshot(data: dict) -> str:
    """Collapse a LOOK/action `data` dict into a compact, human-readable read.

    Lists only what a player actually decides on: where you are, how you're doing,
    what threatens or tempts you nearby, your quests, and anything the owner sent.
    """
    if not isinstance(data, dict) or (data.get("error") and not data.get("surroundings")):
        return f"(no snapshot — {json.dumps(data)[:200]})"
    lines = []
    sur = data.get("surroundings") or {}
    gt = data.get("gameTime") or {}
    wx = (data.get("weather") or {}).get("type", "?")
    zone = sur.get("zoneName", "?")
    pos = f"({sur.get('x')},{sur.get('y')})"
    phase = gt.get("dayPhase", "?")
    diff = sur.get("zoneDifficulty")
    head = f"{zone} {pos} diff{diff} | {phase} {gt.get('season','')} | {wx}"
    if data.get("agent") or data.get("self"):
        who = (data.get("self") or {}).get("name") or (data.get("agent") or {}).get("name")
        if who:
            head = f"[{who}] " + head
    lines.append(head)

    hp = data.get("health") or {}
    en = data.get("energy") or {}
    hu = data.get("hunger") or {}
    vit = (f"HP {hp.get('current')}/{hp.get('max')} "
           f"E {en.get('energy')}/{en.get('maxEnergy')}({_pct(en.get('energy'), en.get('maxEnergy'))}%) "
           f"Hun {hu.get('hunger')}/{hu.get('maxHunger')}({_pct(hu.get('hunger'), hu.get('maxHunger'))}%)")
    flags = []
    if en.get("resting"):
        flags.append("resting")
    if en.get("meditating"):
        flags.append("meditating")
    if data.get("deathPenalty"):
        flags.append("DEATH-PENALTY")
    if data.get("poisoned"):
        flags.append("POISONED")
    if flags:
        vit += " [" + " ".join(flags) + "]"
    lines.append(vit)

    # equipment / activity
    slots = ((data.get("equipment") or {}).get("slots") or {})
    mh = (slots.get("MAIN_HAND") or {}).get("itemId", "empty")
    act = data.get("currentActivity")
    act_s = f"{act.get('interactionType')}->{act.get('targetName')} {int((act.get('progress') or 0)*100)}%" if act else "idle"
    eng = data.get("engaged")
    inv = data.get("inventory") or {}
    line3 = f"MainHand: {mh} | Activity: {act_s} | Inv {inv.get('usedSlots')}/{inv.get('maxSlots')}"
    if eng:
        line3 = f"IN COMBAT with {eng.get('targetName')} | " + line3
    lines.append(line3)

    # nearby creatures split into threats (aggressive) and prey
    ents = sur.get("nearbyEntities") or []
    threats, prey = [], []
    for e in ents:
        if e.get("type") != "CREATURE":
            continue
        ci = e.get("creatureInfo") or {}
        tag = f"{e.get('name')}(L{ci.get('level','?')},{ci.get('currentHealth','?')}hp,d{e.get('distance')})"
        (threats if ci.get("aggressive") else prey).append(tag)
    if threats:
        lines.append("THREATS: " + ", ".join(threats[:8]))
    if prey:
        lines.append("Prey: " + ", ".join(prey[:8]))

    # resources / ground items
    res = [f"{e.get('name')}(d{e.get('distance')})" for e in ents if e.get("type") == "RESOURCE"]
    ground = [f"{e.get('name')}x{e.get('quantity','?')}(d{e.get('distance')})"
              for e in ents if e.get("type") == "GROUND_ITEM"]
    if res:
        lines.append("Resources: " + ", ".join(res[:10]))
    if ground:
        lines.append("Ground: " + ", ".join(ground[:8]))

    # NPCs / shops / quest givers
    npcs = [e.get("name") for e in ents if e.get("type") == "NPC"]
    shops = _field(data, "nearbyShops") or []
    qgivers = _field(data, "nearbyQuestGivers") or []
    social = []
    if npcs:
        social.append(f"NPCs: {', '.join(npcs[:6])}")
    if shops:
        social.append(f"Shops: {len(shops)}")
    if qgivers:
        names = ", ".join(q.get("name", "?") for q in qgivers[:4])
        social.append(f"QuestGivers: {names}")
    if social:
        lines.append(" | ".join(social))

    # other agents nearby
    agents = [f"{e.get('name')}(L{e.get('agentLevel','?')},d{e.get('distance')})"
              for e in ents if e.get("type") == "AGENT"]
    if agents:
        lines.append("Agents: " + ", ".join(agents[:8]))

    # exits / buildings
    exits = _field(data, "nearbyExits") or []
    buildings = _field(data, "nearbyBuildings") or []
    nav = []
    if exits:
        nav.append("Exits: " + ", ".join(f"{x.get('label')}({x.get('x')},{x.get('y')})" for x in exits[:5]))
    if buildings:
        nav.append("Buildings: " + ", ".join(b.get("name", "?") for b in buildings[:6]))
    if nav:
        lines.append(" | ".join(nav))

    # quests
    for q in (data.get("activeQuests") or []):
        objs = " ".join(f"{o.get('current')}/{o.get('required')}{'✓' if o.get('complete') else ''}"
                        for o in (q.get("objectives") or []))
        lines.append(f"Quest {q.get('questId')}: [{objs}]")

    # craftable (only the ones you can actually make right now)
    craftable = [r.get("id") for r in (data.get("craftableRecipes") or []) if r.get("canCraft")]
    if craftable:
        lines.append("CanCraft: " + ", ".join(craftable[:10]))

    # owner instructions + context (highest priority — last so it's most visible)
    instrs = data.get("instructions") or []
    if instrs:
        for i in instrs:
            lines.append(f"⚑ INSTRUCTION [{i.get('id','?')}]: {i.get('message','')}")
    ch = data.get("contextHint")
    if ch:
        lines.append("Hint: " + ch[:280])

    # notable events this tick
    notable = [f"{ev.get('type')}: {ev.get('message','')}"
               for ev in (data.get("events") or [])
               if any(k in (ev.get("type") or "") for k in
                      ("levelup", "killed", "achievement", "quest", "died", "warped",
                       "target_lost", "threshold", "poisoned"))]
    for n in notable[:6]:
        lines.append("• " + n)

    return "\n".join(lines)
