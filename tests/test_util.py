import builtins
import io
from unittest.mock import patch

import util


def test_is_docker_false_when_no_dockerenv_and_no_cgroup(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: False)

    def fake_open(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(builtins, "open", fake_open)
    assert util.is_docker() is False


def test_is_docker_true_when_dockerenv_exists(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: p == "/.dockerenv")
    assert util.is_docker() is True


def test_is_docker_true_when_cgroup_mentions_docker(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: False)

    def fake_open(path, *args, **kwargs):
        if path == "/proc/self/cgroup":
            return io.StringIO("12:/docker/abc123\n")
        raise FileNotFoundError

    monkeypatch.setattr(builtins, "open", fake_open)
    assert util.is_docker() is True


def test_is_docker_handles_unreadable_cgroup(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda p: False)

    def fake_open(*args, **kwargs):
        raise PermissionError

    monkeypatch.setattr(builtins, "open", fake_open)
    assert util.is_docker() is False
