"""End-to-end test of the agent loop + every tool against tests/dummy_game.py,
with a scripted fake model instead of LM Studio (no GPU needed).
Run:  tests\\dummygame.exe tests\\dummy_game.py   (in another console)  then  python tests/test_agent_mock.py"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import agent  # noqa: E402
import tools  # noqa: E402

LOG = ROOT / "tests" / "dummy.log"


def hp():
    return int(LOG.read_text().strip().splitlines()[-1].split()[1])


def call(_tool, **args):
    return {"tool_calls": [{"id": _tool, "type": "function",
                            "function": {"name": _tool, "arguments": json.dumps(args)}}]}


def turn(a, text, steps):
    it = iter(steps)
    reply = a.send(text, complete=lambda: (lambda s: s() if callable(s) else s)(next(it)))
    return reply


def main():
    a = agent.Agent("fake")
    ok = lambda res: "error" not in res

    turn(a, "Сделай трейнер для тестовой игры", [
        call("select_game", path=str(ROOT / "tests")),
        call("set_process", process="dummygame.exe"),
        {"content": "Какое здоровье на экране?"},
    ])
    assert tools.S.process == "dummygame.exe", tools.S.process
    assert not tools.S.blocked

    v1 = hp()
    turn(a, f"здоровье {v1}", [lambda: call("first_scan", value_type="int32", value=hp()),
                              {"content": "Получи урон и скажи новое значение"}])
    time.sleep(2.2)
    turn(a, "получил урон", [lambda: call("next_scan", condition="exact", value=hp()),
                            call("list_candidates", limit=5), {"content": "Нашёл."}])
    meta, addrs = tools.candidates()
    assert len(addrs) >= 1, "scan found nothing"
    addr = f"0x{addrs[0]:X}"
    turn(a, "бесконечное здоровье на F1", [
        call("write_value", address=addr, value_type="int32", value=500),
        call("add_cheat", name="Infinite Health", hotkey="F1", type="freeze",
             target={"address": addr}, value_type="int32", value=999),
        call("test_cheat", name="Infinite Health"),
        call("build_trainer"),
        {"content": "Готово"},
    ])
    for e in tools.S.events:
        print(("OK  " if e["ok"] else "ERR ") + e["tool"].ljust(16) + e["summary"][:150])
    bad = [e for e in tools.S.events if not e["ok"]]
    assert not bad, bad
    test = [e for e in tools.S.events if e["tool"] == "test_cheat"][-1]["summary"]
    assert '"after": 999' in test, test
    print("\nALL OK:", [e for e in tools.S.events if e["tool"] == "build_trainer"][-1]["summary"])


if __name__ == "__main__":
    main()
