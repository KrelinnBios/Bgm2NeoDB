import sys
import threading
import webbrowser
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import uvicorn

import main as launcher
from app import routes
from app.config import ORIGIN


@pytest.fixture
def startup(monkeypatch):
    sock = Mock()
    server = Mock(started=False)
    thread = Mock()
    browser = Mock(return_value=True)
    stopped = threading.Event()
    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr(launcher.socket, "socket", Mock(return_value=sock))
    monkeypatch.setattr(routes, "create_app", Mock(return_value=object()))
    monkeypatch.setattr(uvicorn, "Server", Mock(return_value=server))
    monkeypatch.setattr(launcher.threading, "Thread", thread)
    monkeypatch.setattr(launcher.threading, "Event", lambda: stopped)
    monkeypatch.setattr(launcher.webbrowser, "open", browser)
    server.run.side_effect = lambda **kwargs: thread.call_args.kwargs["target"]()
    return SimpleNamespace(
        sock=sock, server=server, thread=thread, browser=browser, stopped=stopped
    )


@pytest.mark.parametrize("no_browser", [False, True])
def test_slow_startup_reports_progress_and_waits_until_ready(startup, no_browser, capsys):
    if no_browser:
        sys.argv.append("--no-browser")
    waits = 0

    def wait_until_ready(seconds):
        nonlocal waits
        waits += 1
        if waits == 1:
            output = capsys.readouterr().out
            assert "Bgm2NeoDB" in output
            assert ORIGIN not in output
        if waits == 150:
            startup.server.started = True
        assert waits <= 150
        assert seconds == 0.1

    startup.stopped.wait = wait_until_ready
    assert launcher.main() == 0
    assert waits == 150
    assert ORIGIN in capsys.readouterr().out
    if no_browser:
        startup.browser.assert_not_called()
    else:
        startup.browser.assert_called_once_with(ORIGIN)
    assert startup.stopped.is_set()
    startup.sock.close.assert_called_once()


@pytest.mark.parametrize("failure", [False, webbrowser.Error("no browser"), OSError("no browser")])
def test_browser_failure_shows_manual_url(startup, failure, capsys):
    startup.server.started = True
    if isinstance(failure, Exception):
        startup.browser.side_effect = failure
    else:
        startup.browser.return_value = failure
    assert launcher.main() == 0
    # The URL is printed again as a fallback when opening the browser fails.
    assert capsys.readouterr().out.count(ORIGIN) == 2
    startup.browser.assert_called_once_with(ORIGIN)


def test_startup_failure_stops_browser_wait_and_closes_socket(startup, capsys):
    startup.server.run.side_effect = RuntimeError("startup failed")
    with pytest.raises(RuntimeError, match="startup failed"):
        launcher.main()
    assert startup.stopped.is_set()
    startup.thread.call_args.kwargs["target"]()
    startup.browser.assert_not_called()
    startup.sock.close.assert_called_once()
    assert ORIGIN not in capsys.readouterr().out
