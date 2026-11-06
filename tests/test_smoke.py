import socket

import pytest


def test_import():
    import src

    assert src is not None


def test_network_guard():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="network disabled"):
            s.connect(("1.1.1.1", 80))
    finally:
        s.close()
