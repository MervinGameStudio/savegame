# Changelog

## 0.9.1 — Initial release

First release of savegame, a write-through, slot-based key-value savegame
library for the Infernux engine:

- **Write-through by default** — `set` lands on disk the moment the value
  changes; a crash loses at most the last confirmed change. Batch mode
  (`batch()` / `flush()`) is an opt-in primitive for high-frequency writes.
- **Slots** — isolated save spaces; editor and Player domains are naturally
  separated.
- **File-is-value storage** — plain JSON files you can open and hand edit;
  optional slot-level compression and an aggregate layout for many small
  keys, switchable at any time via `migrate`, with a read-only `diagnose()`
  scan for silent anomalies.
- **Savegame Browser editor panel** — staged editing with explicit save
  confirmation, independent slot browsing, virtualized rendering for large
  saves.
- **Eight MCP operations** (`mervingamestudio.savegame.*`) so AI agents can
  drive savegame debugging without probe scripts.
- Zero third-party dependencies. Requires the Infernux engine 0.4.x.
