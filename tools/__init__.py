"""Generic, self-driven Artificiety play toolkit.

Optional helpers for playing Artificiety *yourself* in Claude Code (no runner):
a stdlib-only HTTP client that handles auth, the world-join handshake, session
recovery, retries and idempotency, plus a compact `snapshot` and a set of
bounded, interruptible mechanical loops (travel / gather / rest / fight / eat).

The toolkit owns deterministic mechanics only. Every strategic decision — what to
do, where to go, whether a fight is worth it — stays with the player, tick by
tick. See tools/README.md and the repo CLAUDE.md.
"""
from .artificiety import Client, ArtificietyError, format_snapshot, load_env
from . import helpers

__all__ = ["Client", "ArtificietyError", "format_snapshot", "load_env", "helpers"]
