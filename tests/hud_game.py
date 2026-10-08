"""Stand-in game WITH a window and a HUD, for the memory autopilot (vision reads the HUD, the scanner finds
the numbers). A little arena: the player (blue) walks with the arrow keys, an enemy (red) bounces around.
Health drains slowly, gold changes only when you press G (like money in a real game). Values live in a
ctypes buffer (int32 health, int32 gold, float stamina), redrawn from memory every frame, so a write from
outside shows up on the HUD.
Run:  python -m PyInstaller --onedir --windowed --name HudGame tests/hud_game.py   (one process, like a game)
  or: copy tests\\dummygame.exe tests\\hudgame.exe  then  tests\\hudgame.exe tests\\hud_game.py"""
import ctypes
import struct
import tkinter as tk

buf = ctypes.create_string_buffer(16)
struct.pack_into("<iif", buf, 0, 87, 1520, 63.5)

root = tk.Tk()
root.title("HUD Game")
root.geometry("640x360")
canvas = tk.Canvas(root, width=640, height=360, bg="#2a3b2a", highlightthickness=0)
canvas.pack()
canvas.create_rectangle(0, 0, 640, 44, fill="#111111", outline="")
hud = canvas.create_text(12, 22, anchor="w", font=("Consolas", 16, "bold"), fill="white")
player = canvas.create_rectangle(300, 200, 330, 240, fill="#3a7bd5", outline="white")
enemy = canvas.create_oval(100, 120, 130, 150, fill="#d53a3a", outline="black")
state = {"tick": 0, "ex": 3, "ey": 2}


def frame():
    hp, gold, stamina = struct.unpack_from("<iif", buf, 0)
    state["tick"] += 1
    if state["tick"] % 40 == 0 and hp > 1:      # health drains every 2 s
        hp -= 1
        struct.pack_into("<i", buf, 0, hp)
    canvas.itemconfig(hud, text=f"HP {hp}    GOLD {gold}    STAMINA {stamina:.1f}")
    x0, y0, x1, y1 = canvas.coords(enemy)
    if x0 < 0 or x1 > 640:
        state["ex"] *= -1
    if y0 < 50 or y1 > 360:
        state["ey"] *= -1
    canvas.move(enemy, state["ex"], state["ey"])
    root.after(50, frame)


def spend(_):
    struct.pack_into("<i", buf, 4, struct.unpack_from("<i", buf, 4)[0] - 20)


for key, (dx, dy) in {"<Left>": (-8, 0), "<Right>": (8, 0), "<Up>": (0, -8), "<Down>": (0, 8)}.items():
    root.bind(key, lambda e, dx=dx, dy=dy: canvas.move(player, dx, dy))
root.bind("g", spend)
frame()
root.mainloop()
