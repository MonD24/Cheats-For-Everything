<p align="center"><img src="assets/logo.svg" width="128" alt=""></p>

<h1 align="center">Cheats For Everything</h1>

<p align="center">Point it at a single-player PC game; it finds the values, tests every cheat in the running game and builds a trainer. Runs locally on a vision model in LM Studio.</p>

<p align="center"><a href="README.ru.md">Русская версия</a></p>

---

## What you get

For a Unity (Mono) game, an in-game trainer: BepInEx plugin, an overlay with the cheat list, F1–F12 hotkeys.
For any other engine, a standalone `.exe` trainer that attaches to the game.

Each cheat in the result says how it was checked: in a real fight, by measurement (jump height, distance
walked, the number on the HUD), or by reading the value back. Cheats that fail their check are dropped.

Single-player games only. Games with anti-cheat (EasyAntiCheat, BattlEye, Vanguard, …) and online-only
games are refused. Save folders are backed up before anything touches the game.

## Requirements

- Windows 10/11, Python 3.10+
- [LM Studio](https://lmstudio.ai) with a vision model. Tested with `qwen/qwen3.6-35b-a3b` (recommended;
  mostly runs from system RAM), `qwen/qwen3.5-9b`, `google/gemma-4-12b-qat`.
- The game installed (Steam, Epic or GOG libraries are found automatically; any folder works too)
- Optional: Cheat Engine, for manual pointer/AOB work

```
pip install -r requirements.txt
```

The Unity plugin is compiled on the fly with the C# compiler that ships with Windows
(`C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`); no .NET SDK needed.

## Use

**Web UI.** Run `start.bat`. It starts LM Studio, loads the best installed model and opens
http://localhost:8765. Pick a game on the left (or paste a path to its exe) and write what you want in the
chat. The right panel lists the cheats; "Build .exe" builds the trainer.

**Fully automatic, no UI:**

```
python app/run_auto.py "D:\Games\SomeGame\SomeGame.exe"
```

It installs what is needed, starts the game, gets through the menus to gameplay, finds and tests the cheats
and writes the result to `games/<Game>/`. Add `--no-vision` to run without a model (menus are explored
without screen reading; HUD checks are skipped).

**Output** (in `games/<Game>/`):

| File | What |
|---|---|
| `release/<Game>Trainer.zip` | the trainer and a README with hotkeys and how each cheat was checked |
| `cheats.txt` / `trainer.json` | the cheat list (Unity plugin / exe trainer) |
| `autopilot_report.json` | what was tried, found, kept and dropped, with reasons |
| `playbook.json` | what the harness learned about the game (menu route, player class, values); reruns are faster |

Build or rebuild an exe trainer by hand: `python build_trainer.py games/<Game>/trainer.json`
(`--run-py` runs it without building). Package any trainer: `python -m trainerkit.package games/<Game>`.

## How it works

```
select game ─► engine fingerprint ─► anti-cheat check ─► save backup
                    │
        Unity Mono  │  anything else
     ┌──────────────┴──────────────┐
  reflection plugin           memory route
     │                             │
  menus ─► gameplay            menus ─► gameplay
  find values by experiment    HUD numbers ─► memory scan
  hooks, fun cheats            pointer chains, restart check
  verify (values, fight)       verify on the HUD
     └──────────────┬──────────────┘
              trainer + report
```

**Getting to gameplay.** The menus are explored as a graph of exact screen states. The vision model is
asked only on new screens, with one screenshot and a short list of actions (accept, back, arrows, skip,
or the key a prompt on screen names). Actions that change nothing, or that lead back to a screen already
seen (a toggle, a "back"), are banned on that screen, so the explorer cannot ping-pong. On option pages
(player count, difficulty) confirming only cycles a value, so the explorer walks down to the bottom button
instead. Popups over the game are dismissed with the key they name. The route is saved as screen→action
pairs and replayed by matching screens, so the second run reaches gameplay in about a minute.

**Unity: finding values by experiment.** A BepInEx plugin (`unity/UniversalTrainer.cs`) answers commands
through a file channel. `snap`/`diff`/`find` work like a memory scanner over every numeric field of every
live game object, but with class and field names. Candidates are ranked by the words in their names
(identifiers are split into words) and by structure (a field with a `Max…` sibling is a bar), then tested:
write a value, read it back, and check that the screen changes outside a noise mask built from idle frames.
For money, the harness reads the game's IL: the method that calls the HUD label setter shows which field
the HUD displays and which method redraws it (many HUDs redraw only on game events). The vision model then
reads the number on the HUD to confirm. God mode is a Harmony hook (`CanGetHit → false`, `TakeDamage → skip`)
limited to the player's class. The player is the object that moves when the movement keys are held.

**Unusual cheats.** Developer switches left in the code (invisibility, "uncatchable", infinite special),
super jump and speed (kept only if the measured jump height or distance really grows), one-hit kills and
frozen enemies (the enemy class is the one that shares the player's health field), slow motion and turbo.

**Verification.** Every cheat is switched on through the plugin and checked. Health and god mode get a
real fight: without the cheat the player must take damage, with it not. Checks undo themselves (money added
during a test is taken back, modifiers restore the old value when switched off). While probing, a safety
net (god-mode hooks) keeps the player alive; after a game over the explorer navigates back.

**Other engines: the HUD as the oracle.** The vision model reads the numbers on the HUD (or opens the usual
menu keys — inventory, character sheet — when a number is not on the HUD). A range scan finds that number
in memory; while it changes by itself the scan follows the direction of change; the rest is narrowed by
write-bisection: write a distinct number to half of the candidates, ask whether the HUD shows it, restore.
A pointer scanner turns the heap address into module+offset chains, the game is restarted, and only the
chains that still show the HUD number are kept.

## Engines

| Engine | Route | Status |
|---|---|---|
| Unity (Mono), incl. new Input System | reflection plugin | full pipeline; tested on River City Girls, Pizza Possum |
| Unity (IL2CPP), Unreal, GameMaker, REDengine, native | memory route | values that appear as numbers on the HUD or in a menu |
| anything with anti-cheat, online-only games | — | refused |

Known limits: bars without numbers (health bars in many 3D games) are not found by the memory route yet;
the vision model sometimes misreads which menu item is highlighted (the explorer works around it, but a
setting may get changed on the way); IL2CPP and Unreal have no reflection plugin yet.

## Project layout

| Path | What |
|---|---|
| `app/run_auto.py` | fully automatic run |
| `app/server.py`, `app/static/` | web UI |
| `app/agent.py`, `app/tools.py`, `app/unity_tools.py` | chat agent on LM Studio and its tools |
| `trainerkit/autopilot.py` | Unity pipeline: menus, values, hooks, fun cheats, verification |
| `trainerkit/mem_autopilot.py`, `trainerkit/pointers.py` | memory route and pointer scanner |
| `trainerkit/il_trace.py` | IL reader: what feeds a HUD label |
| `trainerkit/gamescan.py` | game library, engine/anti-cheat fingerprint |
| `trainerkit/vision.py`, `trainerkit/screen.py`, `trainerkit/winshot.py`, `trainerkit/play.py` | vision questions, frame comparison, window capture, keyboard input |
| `trainerkit/runtime.py`, `build_trainer.py` | exe trainer runtime and builder |
| `unity/` | the Unity plugin, its builder and installer, BepInEx 5 |
| `scanner.py` | command-line value scanner |
| `tests/` | tests that need no game or GPU, and two stand-in games |

## Settings

| Variable | Default | |
|---|---|---|
| `LMSTUDIO_URL` | `http://localhost:1234/v1` | LM Studio server |
| `TRAINER_MODEL` | best installed | force a model |
| `TRAINER_GAME_VRAM_GB` | `3` | video memory left free for the game when the model is loaded |
| `TRAINER_REASONING` | `none` | `on` lets the chat agent think before each step (much slower) |
| `TRAINER_PORT` | `8765` | web UI port |

## Tests

```
python tests/test_autopilot_nav.py      # menu explorer against a simulated menu
python tests/test_safe_startup.py       # startup input guard, small screen changes
python tests/test_agent_interrupt.py
python tests/test_learning_memory.py
python tests/test_server_queue.py
```

`tests/hud_game.py` is a small game with a HUD for the memory route
(`python -m PyInstaller --onedir --windowed --name HudGame tests/hud_game.py`).
`tests/test_agent_mock.py` needs `tests/dummy_game.py` running as `dummygame.exe` (a renamed copy of python.exe).

## Third-party

BepInEx 5 (LGPL-2.1) is included unmodified in `unity/vendor/bepinex`; its license is in
`unity/vendor/BepInEx-LICENSE.txt`.
