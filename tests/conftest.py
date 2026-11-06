import socket

import pytest

_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex


def _blocked(sock: socket.socket) -> bool:
    return sock.family in (socket.AF_INET, socket.AF_INET6)


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch):
    def connect(self, address):
        if _blocked(self):
            raise RuntimeError("network disabled in tests")
        return _orig_connect(self, address)

    def connect_ex(self, address):
        if _blocked(self):
            raise RuntimeError("network disabled in tests")
        return _orig_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
