import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))


class StubAgent:
    def __init__(self):
        self.model = "stub"
        self.messages = []
        self.received = []
        self.started = threading.Event()
        self.release_first = threading.Event()
        self.interrupted = False

    def send(self, text):
        self.received.append(text)
        if text == "first":
            self.started.set()
            self.release_first.wait(2)

    def request_interrupt(self):
        self.interrupted = True

    def clear_interrupt(self):
        self.interrupted = False

    def interrupt_requested(self):
        return self.interrupted


import agent  # noqa: E402

agent.Agent = StubAgent
import server  # noqa: E402
import tools  # noqa: E402


class ChatQueueTests(unittest.TestCase):
    def setUp(self):
        self.old_state = server.STATE.copy()
        self.stub = StubAgent()
        server.STATE.update(agent=self.stub, busy=False, queue=[], worker=None)

    def tearDown(self):
        worker = server.STATE.get("worker")
        if worker and worker.is_alive():
            self.stub.release_first.set()
            worker.join(1)
        server.STATE.clear()
        server.STATE.update(self.old_state)

    def test_interrupt_message_is_prioritized_and_processed_next(self):
        first, status = server.queue_chat("first")
        self.assertEqual(status, 202)
        self.assertTrue(self.stub.started.wait(1))

        normal, status = server.queue_chat("normal")
        self.assertEqual(status, 202)
        self.assertEqual(normal["position"], 2)
        correction, status = server.queue_chat("correction", interrupt=True)
        self.assertEqual(status, 202)
        self.assertTrue(correction["interrupt_requested"])
        self.assertTrue(self.stub.interrupt_requested())
        self.assertEqual(server.snapshot()["queued"], 2)
        queued = [m for m in server.snapshot()["chat"] if m.get("queued")]
        self.assertEqual([m["text"] for m in queued], ["correction", "normal"])
        self.assertTrue(queued[0]["interrupt"])

        self.stub.release_first.set()
        worker = server.STATE["worker"]
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.stub.received, ["first", "correction", "normal"])
        self.assertFalse(server.STATE["busy"])

    def test_invalid_and_oversized_messages_are_rejected(self):
        self.assertEqual(server.queue_chat("  ")[1], 400)
        self.assertEqual(server.queue_chat("x" * (server.MAX_MESSAGE_LENGTH + 1))[1], 413)

    def test_explicit_game_hint_is_saved_without_model_tool_call(self):
        original_root, original_game = tools.ROOT, tools.S.game
        try:
            with tempfile.TemporaryDirectory() as folder:
                tools.ROOT = Path(folder)
                tools.S.game = {"name": "Example Game"}
                self.stub.release_first.set()
                result, status = server.queue_chat("зажми H подольше")
                self.assertEqual(status, 202)
                self.assertTrue(result["hint_saved"])
                lesson_path = tools.S.lessons_path()
                self.assertIn("зажми H подольше", lesson_path.read_text(encoding="utf-8"))
                server.STATE["worker"].join(1)
        finally:
            tools.ROOT, tools.S.game = original_root, original_game


if __name__ == "__main__":
    unittest.main()