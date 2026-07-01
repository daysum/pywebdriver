import importlib
import sys
import textwrap
import types
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class FakeBabel:
    def __init__(self, *args, **kwargs):
        pass


def _base_config(rfid_config):
    rfid_config = textwrap.dedent(rfid_config).strip()
    return textwrap.dedent(
        """
        [localization]
        locale=

        [flask]
        host=127.0.0.1
        port=8069
        cors_origins=*
        debug=false
        use_reloader=false

        [application]
        print_status_start=false
        drivers=rfid_driver

        {rfid_config}
        """
    ).format(rfid_config=rfid_config)


def _load_pywebdriver(tmp_path, monkeypatch, rfid_config):
    (tmp_path / "config.ini").write_text(
        _base_config(rfid_config),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(PACKAGE_ROOT))

    fake_flask_babel = types.ModuleType("flask_babel")
    fake_flask_babel.Babel = FakeBabel
    fake_flask_babel.gettext = lambda message, *args, **kwargs: message
    monkeypatch.setitem(sys.modules, "flask_babel", fake_flask_babel)

    import werkzeug

    monkeypatch.setattr(werkzeug, "__version__", "3", raising=False)

    for module_name in list(sys.modules):
        if module_name == "pywebdriver" or module_name.startswith("pywebdriver."):
            del sys.modules[module_name]

    return importlib.import_module("pywebdriver")


def test_rfid_read_returns_normalized_simulator_tags_once(tmp_path, monkeypatch):
    pywebdriver = _load_pywebdriver(
        tmp_path,
        monkeypatch,
        """
        [rfid_driver]
        mode=simulator
        simulator_tags=
            E2 00-0017:2211 0144 1890 abcd,
            E2000017221101441890ABCD,
            300833B2DDD9014000000001
        max_tags_per_poll=200
        """,
    )

    client = pywebdriver.app.test_client()
    response = client.post("/hw_proxy/rfid_read", json={"cursor": "0"})

    assert response.status_code == 200
    payload = response.get_json()
    result = payload["result"]
    assert payload["jsonrpc"] == "2.0"
    assert result["info"] == "ok"
    assert [tag["epc"] for tag in result["tags"]] == [
        "E2000017221101441890ABCD",
        "300833B2DDD9014000000001",
    ]

    next_response = client.post(
        "/hw_proxy/rfid_read",
        json={"cursor": result["cursor"]},
    )

    next_result = next_response.get_json()["result"]
    assert next_result["tags"] == []
    assert next_result["cursor"] == result["cursor"]


def test_rfid_read_supports_jsonrpc_params_and_bounded_batches(tmp_path, monkeypatch):
    pywebdriver = _load_pywebdriver(
        tmp_path,
        monkeypatch,
        """
        [rfid_driver]
        mode=simulator
        simulator_tags=
        max_tags_per_poll=2
        """,
    )

    client = pywebdriver.app.test_client()
    response = client.post(
        "/hw_proxy/rfid_read",
        json={
            "jsonrpc": "2.0",
            "id": "request-1",
            "params": {
                "cursor": "0",
                "tags": ["aa-aa", "bb:bb", "cc cc"],
            },
        },
    )

    payload = response.get_json()
    result = payload["result"]
    assert payload["id"] == "request-1"
    assert [tag["epc"] for tag in result["tags"]] == ["AAAA", "BBBB"]
    assert result["has_more"] is True

    next_result = client.post(
        "/hw_proxy/rfid_read",
        json={"cursor": result["cursor"]},
    ).get_json()["result"]
    assert [tag["epc"] for tag in next_result["tags"]] == ["CCCC"]
    assert next_result["has_more"] is False


def test_disabled_rfid_driver_returns_empty_successful_batch(tmp_path, monkeypatch):
    pywebdriver = _load_pywebdriver(
        tmp_path,
        monkeypatch,
        """
        [rfid_driver]
        mode=disabled
        simulator_tags=
        max_tags_per_poll=200
        """,
    )

    client = pywebdriver.app.test_client()
    response = client.post(
        "/hw_proxy/rfid_read",
        json={"tags": ["E2000017221101441890ABCD"]},
    )

    result = response.get_json()["result"]
    assert result["info"] == "ok"
    assert result["tags"] == []
    assert pywebdriver.drivers["rfid"].get_status()["status"] == "disconnected"


def test_rfid_line_parser_buffers_partial_epc_lines(tmp_path, monkeypatch):
    _load_pywebdriver(
        tmp_path,
        monkeypatch,
        """
        [rfid_driver]
        mode=disabled
        max_tags_per_poll=200
        """,
    )
    rfid_driver = sys.modules["pywebdriver.plugins.rfid_driver"]

    complete, pending = rfid_driver.split_complete_lines([b"E200"])
    assert complete == []
    assert pending == b"E200"

    complete, pending = rfid_driver.split_complete_lines([b"0017\n3008"], pending)
    assert complete == [b"E2000017\n"]
    assert pending == b"3008"

    complete, pending = rfid_driver.split_complete_lines([b"33\r\n"], pending)
    assert complete == [b"300833\r\n"]
    assert pending == b""
