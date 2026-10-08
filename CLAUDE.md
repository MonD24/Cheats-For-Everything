# Cheats For Everything — notes for Claude Code

The user names a game; the goal is a working trainer with every cheat verified in the running game.
Answer the user in the language they write in.

## 0. Hard rules
- **Single-player / offline only.** `python -m trainerkit.gamescan "<game>"` reports engine and anti-cheat.
  Online/competitive games or anti-cheat (EAC, BattlEye, Vanguard, VAC-secured servers, Ricochet, nProtect,
  Denuvo Anti-Cheat): refuse, offer an offline mode or a regular mod instead.
- Only games the user owns and has installed. Never bypass DRM, launcher ownership checks or anti-cheat.
- Back up saves first (the `backup_saves` tool zips them to `~/.cheats-for-everything/backups`).
- Never add game-specific code to the harness: fix the general rule; game facts belong in the playbook.

## 1. First try the autonomous route
`python app/run_auto.py "<path to Game.exe>"` (see README "How it works"). Read
`games/<game>/autopilot_report.json`; when it fails, find the general cause, fix the harness, rerun from a
clean `games/<game>` folder. A fix counts only when a fresh solo run succeeds.
- Unity Mono → `trainerkit/autopilot.py` + `unity/UniversalTrainer.cs` (reflection snapshots, experiments,
  IL trace of HUD labels, Harmony hooks, fight test).
- Other engines → `trainerkit/mem_autopilot.py` (HUD read by the vision model, range scan, directional
  rescans, write-bisection, pointer chains kept only if they survive a game restart).
- Offline tests: `tests/test_autopilot_nav.py` (menu simulator), `tests/hud_game.py` (HUD stand-in game).

## 2. Manual route (when the user wants to drive)
Ask which cheats (health, stamina/mana, ammo, money, one-hit kills, god mode, speed, timer, XP, items,
cooldowns, teleport) and the hotkeys. Then, with the game running:
```
python scanner.py first <Game.exe> int32 <shown value> [tol]   # float for most health/stamina
python scanner.py next exact <new value>     # or decreased / increased / changed / unchanged
python scanner.py list
python scanner.py write 0x... int32 9999     # confirm on screen
python scanner.py info 0x...                 # static (module+offset) or heap
```
Heap addresses need a pointer chain (`trainerkit/pointers.py`, then restart the game and keep the chains
that still resolve) or a code AOB (Cheat Engine: "Find out what writes to this address"). Check AOB
uniqueness: `python scanner.py aob <Game.exe> "<pattern>" <module>` must say unique.

## 3. trainer.json → games/<game>/trainer.json
See the `trainerkit/runtime.py` docstring. Types: `freeze`, `set`, `add`, `patch`. Targets: `address`,
`module`+`offset`+`pointers`, `chains` (first that resolves), `aob` (+`aob_module`, `aob_offset`,
`rip_relative`, `offset`, `pointers`). `"uac_admin": true` if the game runs elevated.

## 4. Verify and hand over
- `python build_trainer.py games/<game>/trainer.json --run-py`, press each hotkey, confirm on screen and
  by reading the value back; restart the game once and test again; patches must restore on OFF.
- `python build_trainer.py games/<game>/trainer.json` → `games/<game>/dist/<Title>.exe`;
  `python -m trainerkit.package games/<game>` → zip + README.
- Tell the user which cheats, hotkeys, what was verified and how, what was not, and that antivirus may flag
  PyInstaller trainers (false positive; the source is next to the exe).

## Lessons
- An AOB can match copies of itself (strings, source text in memory): always run the uniqueness check.
- `float` vs `int32`: if an exact int scan finds nothing, the game likely stores a float.
- Keep cheats harmless to saves: prefer freeze over absurd values (999999 can overflow).
- HUDs often redraw only on game events: a correct field write shows nothing until the game's refresh
  method runs. Follow the label setter in IL to find both the field and the refresh method.
- Name heuristics must split identifiers into words: "stamina" contains "min", "property" contains "per".
- Many games stop rendering while unfocused: focus the window before any screen experiment.
- Verification must undo itself: one-shot cheats are rolled back, modifier freezes restore the old value
  on OFF, enemy HP freezes use "!value" (no restore).
- csc embeds a new MVID in every build: compare the plugin by source hash (`BepInEx/plugins/*.src`).
- Newer Unity: reference `netstandard.dll` when compiling; legacy `UnityEngine.Input` throws under the new
  Input System (the plugin reads hotkeys through Win32).
- Launch stubs (Unreal `Game.exe` → `Game-Win64-Shipping.exe`, launchers) hand over to a child process:
  track the process that owns the window.
- From VS Code terminals `ELECTRON_RUN_AS_NODE=1` stops LM Studio from starting; unset it.
- Qwen 3.x answers in ~2 s with `reasoning_effort: "none"` and ~75 s without it.
