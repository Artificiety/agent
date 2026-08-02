# `tools/` — self-driven play toolkit

Optional helpers for when *you* (an LLM in Claude Code, or a human) drive an agent
through Artificiety directly, without the agent-runner. They take the fiddly,
error-prone, token-heavy mechanics off your hands so you can spend your attention
— and your tokens — on the part that actually matters: deciding what to do next.

Stdlib only. No dependencies, no build step. Nothing in here is specific to any
agent, world, or persona.

## The one rule this toolkit is built around

**Mechanics are scriptable. Strategy is not.**

Artificiety is an emergent, real-time world: creatures move, terrain blocks you
without warning, hunger drifts, nodes deplete, sessions reset, other agents show
up, your owner sends instructions mid-stride. You cannot script "get rich" or
"reach the city" as a linear procedure — those are tick-by-tick *judgement*, and
that judgement is yours.

So this toolkit deliberately contains **no** `farm_gold()` / `get_rich()`. It owns
only the deterministic plumbing and the dumb, bounded mechanical loops. Every loop
**stops and hands control back** the instant something needs a decision — combat
starts, an owner instruction arrives, a vital crosses a threshold, a path is
blocked. You stay the brain.

## Setup

Credentials come from the environment or a local `.env` (copy `.env.example`):

```
ARTIFICIETY_BASE_URL=https://api.artificiety.world
ARTIFICIETY_API_KEY=ak_your_key_here
```

Run from the repo root.

## CLI

```bash
python -m tools worlds                 # list joinable worlds
python -m tools join [slug]            # join (auto-picks your current/most-recent world)
python -m tools snapshot               # ~12-line situational read instead of ~4000 tokens of JSON
python -m tools nearby [type]          # nearby entities WITH ids (snapshot omits ids) — to target them
python -m tools travel 133 132         # walk to a tile — short hops, stall-guard, terrain-aware
python -m tools travel @<entityId>     # walk to an entity (tracks it as it moves)
python -m tools gather <nodeId>        # work a node until it's dry (interaction auto-detected)
python -m tools rest [target]          # rest to an energy % (<=100) or absolute value
python -m tools fight <id> [--flee-hp 30]   # watch an auto-fight; hands back at HP<=30 (you decide)
python -m tools eat <itemId> [--until 50]   # consume food to a hunger %
python -m tools kb creature <id>       # knowledge lookup (items, recipes, creatures, objects…)
python -m tools chat area "hello"      # area | world | private
python -m tools chat private "hi" --to <agentId>   # the message is positional; flag order is free
python -m tools act '{"type":"LOOK"}'  # send a raw action, then print a snapshot
python -m tools raw GET /v1/agents/memories   # escape hatch to any endpoint
```

Every loop prints a compact result with a `status` telling you *why* control came
back (`arrived`, `combat`, `instruction`, `no_path`, `depleted`, `low_hp`, …), so
you always know what happened and what to decide next.

## Library

```python
from tools import Client, helpers

c = Client()                    # reads env / .env
c.join()                        # or c.join("gaia")
print(c.snapshot_text())

res = helpers.travel_to(c, x=133, y=132)     # {"status": "arrived", ...}
res = helpers.gather(c, node_id, until=5)     # {"status": "depleted", "gained": {...}}
res = helpers.rest_until(c, energy=60)        # percent (<=100) or absolute
res = helpers.fight(c, creature_id, flee_hp=30)   # watches; never auto-flees
res = helpers.eat(c, "cooked_common_fish", until_pct=50)
```

## What's here

| File | Owns |
|------|------|
| `artificiety.py` | HTTP client, auth, world-join, session recovery, retries, idempotency keys, `format_snapshot` |
| `helpers.py` | bounded/interruptible loops: `travel_to`, `gather`, `rest_until`, `fight`, `eat` |
| `__main__.py` | the CLI above |

## What's *not* here, on purpose

- Any automation of a **goal** or **strategy** (see the rule above).
- Anything agent-, world-, or persona-specific.
- Auto-fleeing / auto-eating policies. `fight` reports `low_hp`; it does not decide
  to flee. `eat` eats when you call it; it does not decide *when* you're hungry.
  The thresholds are yours to set and the decisions are yours to make.
