import asyncio
import signal
from unittest.mock import patch

from NightCityBot.bot import register_shutdown, NightCityBot, keep_alive, run_flask

class DummyBot(NightCityBot):
    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()


def test_register_shutdown():
    bot = DummyBot()
    def dummy_task(coro):
        coro.close()
        return None

    with patch('signal.signal') as sig_patch, \
         patch.object(bot.loop, 'create_task', side_effect=dummy_task) as create_task:
        register_shutdown(bot)
        assert sig_patch.call_count == 2
        signals = {call.args[0] for call in sig_patch.call_args_list}
        assert signal.SIGINT in signals and signal.SIGTERM in signals
        handler = sig_patch.call_args_list[0].args[1]
        handler(signal.SIGTERM, None)
        create_task.assert_called()


def test_run_flask_uses_port_env(monkeypatch):
    captured = {}

    def fake_run(*, host, port, debug, use_reloader):
        captured.update({
            "host": host,
            "port": port,
            "debug": debug,
            "use_reloader": use_reloader,
        })

    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setattr("NightCityBot.bot.app.run", fake_run)

    run_flask()

    assert captured == {
        "host": "0.0.0.0",
        "port": 8080,
        "debug": False,
        "use_reloader": False,
    }


def test_keep_alive_respects_disable_flag(monkeypatch):
    monkeypatch.setenv("DISABLE_KEEP_ALIVE", "true")

    started = {"value": False}

    class _Thread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            started["value"] = True

    monkeypatch.setattr("NightCityBot.bot.Thread", _Thread)

    assert keep_alive() is False
    assert started["value"] is False


def test_healthcheck_endpoints_return_ok():
    client = __import__("NightCityBot.bot", fromlist=["app"]).app.test_client()

    home = client.get("/")
    healthz = client.get("/healthz")
    readyz = client.get("/readyz")

    assert home.status_code == 200
    assert b"Bot is alive" in home.data
    assert healthz.status_code == 200
    assert healthz.get_json() == {"status": "ok"}
    assert readyz.status_code == 200
    assert readyz.get_json() == {"status": "ready"}
