# ADR-0001 — ABCs, not Protocols, for ports

**Status:** Accepted

## Context

Ports were initially specified as `typing.Protocol` for structural typing and
maximum decoupling — an adapter satisfies a port without importing it.

## Decision

All ports are `abc.ABC` with `@abstractmethod`. One consistent idiom across
ports, the `Row`/`RowProvider` hierarchy, and any future extension point.

## Consequences

**Gained:**

- **Fail-fast at instantiation.** A missing method raises `TypeError` at
  startup, not at the call site or only when a type checker runs.
- **Shared behaviour.** A `BaseHTTPAdapter` carries httpx lifecycle,
  retry/backoff, and rate limiting for the Emby and TMDb adapters instead of
  each reimplementing it. Protocols cannot hold implementation.
- **Discoverability.** `__subclasses__()` and explicit inheritance make "what
  implements this?" answerable without grepping.

**Given up:**

- Structural satisfaction — a third-party class can no longer satisfy a port
  without inheriting from it.

That loss is theoretical here. Every adapter in this codebase is written
deliberately against its port; nothing external needs to satisfy one by
coincidence. The main argument for Protocols therefore buys nothing, while ABC's
fail-fast behaviour and shared-behaviour slot are immediately useful.

Test fakes are unaffected — duck typing works in tests either way.
