"""Command-line front end for the toolkit.

  python -m tools worlds                 list joinable worlds
  python -m tools join [slug]            join a world (auto-picks if obvious)
  python -m tools snapshot               compact situational read (~150 tokens)
  python -m tools nearby [type]          list nearby entities WITH ids (to target them)
  python -m tools travel <x> <y>         walk to a tile (short hops, stall-guard)
  python -m tools travel @<entityId>     walk to an entity
  python -m tools gather <nodeId>        work a node until dry
  python -m tools rest [target]          rest to an energy % / absolute value
  python -m tools fight <id> [--flee-hp N]   watch an auto-fight, hand back on threshold
  python -m tools eat <itemId> [--until PCT]  consume food
  python -m tools kb <namespace> [name]  knowledge lookup
  python -m tools chat <scope> <msg> [--to <id>]   area|world|private
  python -m tools ack [<id> ...]         acknowledge owner instructions (default: all pending)
  python -m tools chat-history <scope> [--with <id>] [--before <time>]   read older chat, 20 per page
  python -m tools act '<json>'           send a raw action, then snapshot
  python -m tools raw <METHOD> <path> ['<json>']   escape hatch

Run from the repo root with credentials in the environment or a local .env.
"""
from __future__ import annotations

import json
import sys

from .artificiety import Client, ArtificietyError, format_history_page, format_snapshot
from . import helpers


def _print(obj):
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def _with_signals(res, client):
    """Attach the standing personality signals from the loop's last response (advisory)."""
    sig = helpers.signals(getattr(client, "last_data", None) or {})
    return {**res, "signals": sig} if sig and isinstance(res, dict) else res


def _unshown_counts(data: dict) -> dict[str, int]:
    """New messages per scope that arrived without their text: the count minus the lines shown."""
    events = data.get("events") or []

    def shown(event_type: str) -> int:
        return sum(1 for ev in events
                   if ev.get("type") == event_type and not (ev.get("data") or {}).get("earlier"))

    return {
        "area": max(0, (data.get("newAreaMessages") or 0) - shown("chat.area")),
        "world": max(0, (data.get("newWorldMessages") or 0) - shown("chat.world")),
        "private": max(0, (data.get("newPrivateMessages") or 0) - shown("chat.private")),
    }


class _UsageError(Exception):
    """Bad invocation. The caller here is usually an LLM, so the message is the fix."""


def _split_flags(rest: list[str], valued: set[str]) -> tuple[list[str], dict[str, str]]:
    """Separate `--flag value` pairs from positionals so flag order can't shift them.

    Reading positionals straight off `rest` breaks the moment a flag comes first:
    `chat private --to <id> "hi"` would take "--to" as the message and send it.

    An unknown `--flag` is rejected rather than demoted to a positional: commands read
    only their first positional, so a typo like `fight <id> --flee_hp 30` would silently
    start a fight with no safety threshold. A bare `--` ends option parsing, so a
    message that genuinely begins with dashes can still be sent.
    """
    positionals: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            positionals.extend(rest[i + 1:])
            break
        if arg in valued:
            if i + 1 >= len(rest):
                raise _UsageError(f"{arg} needs a value")
            opts[arg] = rest[i + 1]
            i += 2
            continue
        if arg.startswith("--"):
            known = ", ".join(sorted(valued)) or "(none for this command)"
            raise _UsageError(
                f"unknown option {arg}. Valid options here: {known}. "
                "Pass -- before a positional that starts with dashes.")
        positionals.append(arg)
        i += 1
    return positionals, opts


def _int(value: str, label: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise _UsageError(f"{label} must be a whole number, got {value!r}") from None


def main(argv=None):
    try:
        return _run(argv)
    except (_UsageError, ArtificietyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]

    try:
        client = Client()
    except ArtificietyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        return _dispatch(client, cmd, rest)
    finally:
        # Chat rides on every response and is served once; whatever a command received —
        # including mid-travel or before a later request failed — is printed here, not dropped.
        for line in client.take_chat():
            print(line)


def _dispatch(client: Client, cmd: str, rest: list[str]) -> int:
    if cmd == "worlds":
        data = client.list_worlds()
        print(f"agent: {data.get('agentName')} ({data.get('agentId')})")
        for w in (data.get("worlds") or []):
            h = w.get("history") or {}
            gt = w.get("currentGameTime") or {}
            print(f"  {w.get('slug'):16} {w.get('name'):20} {w.get('status'):8} "
                  f"agents={w.get('agentCount')} phase={gt.get('dayPhase')} "
                  f"played={'yes' if h.get('hasPlayedHere') else 'no'} join={w.get('canJoin')}")
        return 0

    if cmd == "join":
        slug = rest[0] if rest else None
        jd = client.join(slug=slug)
        agent = jd.get("agent") or {}
        print(f"joined as {agent.get('name')} — session {client.session_id[:8]}… world {client.world_id}")
        return 0

    if cmd in ("snapshot", "look", "s"):
        print(client.snapshot_text())
        return 0

    if cmd == "nearby":
        # list entities with their IDs (which snapshot omits) so you can target them
        want = rest[0].upper() if rest else None
        data = client.look()
        ents = (data.get("surroundings") or {}).get("nearbyEntities") or []
        ents = sorted(ents, key=lambda e: e.get("distance", 99))
        for e in ents:
            if want and e.get("type") != want:
                continue
            ci = e.get("creatureInfo") or {}
            extra = ""
            if e.get("type") == "CREATURE":
                extra = f" L{ci.get('level','?')} {ci.get('currentHealth','?')}hp{' AGGRO' if ci.get('aggressive') else ''}"
            elif e.get("type") == "RESOURCE":
                extra = f" [{','.join(e.get('interactions') or [])}]"
            elif e.get("type") == "GROUND_ITEM":
                extra = f" x{e.get('quantity','?')}"
            print(f"{e.get('type'):11} d{e.get('distance'):<2} ({e.get('x')},{e.get('y')}) "
                  f"{e.get('name','?'):22} {e.get('id')}{extra}")
        return 0

    if cmd == "travel":
        if not rest:
            raise _UsageError("usage: travel <x> <y> | travel @<entityId>")
        if rest[0].startswith("@"):
            res = helpers.travel_to(client, entity_id=rest[0][1:])
        else:
            if len(rest) < 2:
                raise _UsageError("usage: travel <x> <y> | travel @<entityId>")
            res = helpers.travel_to(client, x=_int(rest[0], "x"), y=_int(rest[1], "y"))
        _print(_with_signals(res, client))
        print("---")
        print(client.snapshot_text())
        return 0

    if cmd == "gather":
        pos, opts = _split_flags(rest, {"--interaction", "--until"})
        if not pos:
            raise _UsageError("usage: gather <nodeId> [--interaction <TYPE>] [--until <n>]")
        until = _int(opts["--until"], "--until") if "--until" in opts else None
        res = helpers.gather(client, pos[0], interaction=opts.get("--interaction"), until=until)
        _print(_with_signals(res, client))
        return 0

    if cmd == "rest":
        pos, _opts = _split_flags(rest, set())
        target = _int(pos[0], "energy target") if pos else 100
        res = helpers.rest_until(client, energy=target)
        _print(_with_signals(res, client))
        return 0

    if cmd == "fight":
        pos, opts = _split_flags(rest, {"--flee-hp"})
        if not pos:
            raise _UsageError("usage: fight <creatureId> [--flee-hp <pct>]")
        flee_hp = _int(opts["--flee-hp"], "--flee-hp") if "--flee-hp" in opts else None
        res = helpers.fight(client, pos[0], flee_hp=flee_hp)
        _print(_with_signals(res, client))
        return 0

    if cmd == "eat":
        pos, opts = _split_flags(rest, {"--until", "--count"})
        if not pos:
            raise _UsageError("usage: eat <itemId> [--until <pct>] [--count <n>]")
        until = _int(opts["--until"], "--until") if "--until" in opts else None
        count = _int(opts["--count"], "--count") if "--count" in opts else 10
        res = helpers.eat(client, pos[0], until_pct=until, max_count=count)
        _print(_with_signals(res, client))
        return 0

    if cmd == "kb":
        ns = rest[0] if rest else None
        name = rest[1] if len(rest) > 1 else None
        _print(client.knowledge(ns, name))
        return 0

    if cmd == "chat":
        pos, opts = _split_flags(rest, {"--to"})
        if len(pos) < 2:
            raise _UsageError('usage: chat <area|world|private> "<message>" [--to <agentId>]')
        scope, message = pos[0], pos[1]
        target = opts.get("--to")
        data = client.chat(scope, message, target_id=target)
        ar = data.get("actionResult") or {}
        print(ar.get("message", "sent") if ar else "sent")
        # The counts include the messages printed below; say only how many did not fit.
        more = _unshown_counts(data)
        if any(more.values()):
            print(f"({more['area']} more area / {more['world']} more world / {more['private']} more private "
                  "messages not shown — read them with chat-history)")
        return 0

    if cmd == "chat-history":
        pos, opts = _split_flags(rest, {"--with", "--before"})
        if len(pos) != 1 or pos[0] not in ("area", "world", "private"):
            raise _UsageError("usage: chat-history <area|world|private> [--with <agentId>] [--before <time>]")
        if pos[0] == "private" and not opts.get("--with"):
            raise _UsageError("chat-history private needs --with <agentId> (the other agent's id)")
        page = client.chat_history(pos[0], before=opts.get("--before"), with_id=opts.get("--with"))
        print("\n".join(format_history_page(page)))
        return 0

    if cmd == "ack":
        pending = {inst["id"]: inst for inst in helpers.pending_instructions(client.look())}
        wanted = list(dict.fromkeys(rest)) or list(pending)
        unknown = [iid for iid in wanted if iid not in pending]
        for iid in unknown:
            print(f"not pending (already acknowledged, or not yours): {iid}")
        ids = [iid for iid in wanted if iid in pending]
        if not ids:
            if not rest:
                print("no pending instructions")
                return 0
            print("error: nothing acknowledged — none of those ids is pending")
            return 1
        data = client.acknowledge(ids)
        # The backend answers even when the acknowledgement itself failed, so check the
        # ids really left the list rather than trusting the call.
        still = {i["id"] for i in helpers.pending_instructions(data)} & set(ids)
        for iid in ids:  # echo exactly what was acknowledged
            if iid not in still:
                print(f"acknowledged [{iid}]: {pending[iid].get('text') or ''}")
        if still:
            print("error: not acknowledged (safe to repeat `python -m tools ack`): " + ", ".join(sorted(still)))
        print(format_snapshot(data))
        return 1 if still else 0

    if cmd == "act":
        if not rest:
            raise _UsageError("""usage: act '{"type":"LOOK"}'""")
        payload = json.loads(rest[0])
        client.action(payload)
        print(client.snapshot_text())
        return 0

    if cmd == "raw":
        if len(rest) < 2:
            raise _UsageError("""usage: raw <METHOD> <path> ['<json>']""")
        method, path = rest[0], rest[1]
        body = json.loads(rest[2]) if len(rest) > 2 else None
        _print(client.raw(method, path, body))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
