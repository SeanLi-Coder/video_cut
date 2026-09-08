from __future__ import annotations

import base64
import io
import json
import os
import socket
import threading
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request

import pytest
from fastapi.testclient import TestClient

import app.download_proxy as proxy_module
import app.main as main_module
from app.ai_enhance import (
    AIEnhancementJob,
    AIEnhancementManager,
    AIModelDownloadJob,
    AIResolutionTarget,
)
from app.download_proxy import (
    ProxyConfigurationError,
    build_download_proxy,
    direct_proxy_payload,
    download_proxy_from_storage,
    open_network_request,
    proxy_process_environment,
    redact_proxy_credentials,
    stage_pysocks_module,
    updated_download_proxy,
)
from app.main import ApplicationState, create_app
from app.media import MediaError, VideoSource
from app.storage import SettingsStore, SettingsStoreError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://127.0.0.1:7897", "http://127.0.0.1:7897"),
        ("https://Proxy.Example:443/", "https://proxy.example:443"),
        ("socks5://127.0.0.1:1080/", "socks5://127.0.0.1:1080"),
        ("socks5h://[::1]:7897", "socks5h://[::1]:7897"),
    ],
)
def test_proxy_endpoint_is_validated_and_normalized(raw: str, expected: str) -> None:
    assert build_download_proxy(url=raw).endpoint_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "127.0.0.1:7897",
        "ftp://127.0.0.1:7897",
        "socks5://127.0.0.1",
        "socks5://user:secret@127.0.0.1:7897",
        "http://127.0.0.1:7897/path",
        "http://127.0.0.1:7897?x=1",
        "http://127.0.0.1:99999",
        "http://bad host:7897",
        "http://bad\\host:7897",
        "http://127.0.0.1:7897\nInjected",
    ],
)
def test_proxy_endpoint_rejects_unsafe_or_incomplete_values(raw: str) -> None:
    with pytest.raises(ProxyConfigurationError):
        build_download_proxy(url=raw)


def test_proxy_credentials_are_encoded_for_transport_and_masked_for_display() -> None:
    proxy = build_download_proxy(
        url="socks5h://127.0.0.1:7897",
        username="name@example.com",
        password="p@ss:/?#% word",
    )

    assert proxy.transport_url == (
        "socks5h://name%40example.com:p%40ss%3A%2F%3F%23%25%20word@127.0.0.1:7897"
    )
    public = json.dumps(proxy.public_payload(), ensure_ascii=False)
    assert proxy.password not in public
    assert proxy.transport_url not in public
    assert proxy.public_payload()["has_password"] is True
    assert "••••" in proxy.display_url


def test_keep_password_is_only_allowed_for_the_same_endpoint_and_username() -> None:
    current = build_download_proxy(
        url="http://127.0.0.1:7897",
        username="alice",
        password="secret",
    )
    kept = updated_download_proxy(
        current=current,
        url=current.endpoint_url,
        username="alice",
        password="",
        password_action="keep",
    )
    assert kept.password == "secret"

    with pytest.raises(ProxyConfigurationError):
        updated_download_proxy(
            current=current,
            url="http://127.0.0.1:7898",
            username="alice",
            password="",
            password_action="keep",
        )


def test_settings_updates_do_not_overwrite_other_keys(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"output_directory": "/tmp/output"})
    store.update(
        {"ai_download_proxy": build_download_proxy(url="socks5://127.0.0.1:7897").storage_payload()}
    )
    store.update({"ai_download_proxy": None})

    assert store.load() == {"output_directory": "/tmp/output"}
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o077 == 0


def test_settings_update_refuses_to_overwrite_an_unreadable_payload(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    original = b'{"output_directory": "kept", invalid}'
    settings_path.write_bytes(original)
    store = SettingsStore(settings_path)

    with pytest.raises(SettingsStoreError, match="could not be safely updated"):
        store.update({"ai_download_proxy": {"password": "secret"}})

    assert settings_path.read_bytes() == original


def test_proxy_api_reports_corrupt_settings_without_echoing_password(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    settings_path = tmp_path / "settings.json"
    original = b"not-json"
    settings_path.write_bytes(original)
    state = ApplicationState(
        settings_path=settings_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    sentinel = "must-never-be-returned"

    with TestClient(create_app(state)) as client:
        token = client.get("/api/bootstrap").json()["app_token"]
        response = client.put(
            "/api/ai-download-proxy",
            headers={"X-App-Token": token},
            json={
                "url": "http://127.0.0.1:7897",
                "username": "alice",
                "password": sentinel,
                "password_action": "replace",
            },
        )

    assert response.status_code == 503
    assert sentinel not in response.text
    assert settings_path.read_bytes() == original


def test_invalid_stored_proxy_is_ignored() -> None:
    assert download_proxy_from_storage({"url": "file:///tmp/socket"}) is None
    assert direct_proxy_payload()["configured"] is False


def test_network_opener_uses_explicit_direct_http_and_socks_modes(monkeypatch) -> None:
    captured: list[tuple[object, ...]] = []

    class Response(io.BytesIO):
        status = 200
        headers: dict[str, str] = {}

        def read(self, amount=-1, *, decode_content=False):
            del decode_content
            return super().read(-1 if amount is None else amount)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class Opener:
        def open(self, request, timeout):
            captured.append(("open", request.full_url, timeout))
            return Response(b"ok")

    def fake_build_opener(*handlers):
        captured.append(tuple(handlers))
        return Opener()

    class ProxyManager:
        def __init__(self, proxy_url, **kwargs):
            captured.append(("manager", proxy_url, kwargs))

        def request(self, method, url, **kwargs):
            captured.append(("request", method, url, kwargs))
            return Response(b"ok")

        def clear(self):
            captured.append(("clear",))

    monkeypatch.setattr(proxy_module, "build_opener", fake_build_opener)
    monkeypatch.setattr(proxy_module.urllib3, "ProxyManager", ProxyManager)
    request = Request("https://example.com/file")

    with open_network_request(request, timeout=3, proxy=None):
        pass
    assert isinstance(captured[0][0], ProxyHandler)
    assert captured[0][0].proxies == {}

    captured.clear()
    http_proxy = build_download_proxy(
        url="http://127.0.0.1:7897",
        username="alice",
        password="secret",
    )
    with open_network_request(request, timeout=4, proxy=http_proxy):
        pass
    assert captured[0][0:2] == ("manager", "http://127.0.0.1:7897")
    assert captured[0][2]["proxy_headers"] == {"proxy-authorization": "Basic YWxpY2U6c2VjcmV0"}
    assert "secret" not in captured[0][1]
    assert captured[1][0:3] == ("request", "GET", "https://example.com/file")
    assert captured[-1] == ("clear",)

    captured.clear()
    socks_arguments: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_socks_handler(*args, **kwargs):
        socks_arguments.append((args, kwargs))
        return object()

    monkeypatch.setattr(proxy_module, "SocksiPyHandler", fake_socks_handler)
    socks_proxy = build_download_proxy(
        url="socks5h://127.0.0.1:7897",
        username="alice",
        password="secret",
    )
    with open_network_request(request, timeout=5, proxy=socks_proxy):
        pass
    assert isinstance(captured[0][0], ProxyHandler)
    assert captured[0][0].proxies == {}
    assert socks_arguments[0][1] == {
        "rdns": True,
        "username": "alice",
        "password": "secret",
    }


def test_https_proxy_starts_with_tls_even_when_no_proxy_would_bypass(
    monkeypatch,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(3)
    port = int(listener.getsockname()[1])
    received: list[bytes] = []

    def capture_first_packet() -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                connection.settimeout(3)
                received.append(connection.recv(4_096))
        finally:
            listener.close()

    worker = threading.Thread(target=capture_first_packet)
    worker.start()
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    proxy = build_download_proxy(url=f"https://127.0.0.1:{port}")

    with pytest.raises(URLError, match="proxy request failed"):
        open_network_request(
            Request("https://example.com/model.bin"),
            timeout=2,
            proxy=proxy,
        )
    worker.join(timeout=4)

    assert not worker.is_alive()
    assert received and received[0].startswith(b"\x16\x03")
    assert b"CONNECT" not in received[0]


def test_proxy_process_environment_is_explicit_and_uses_isolated_pysocks(
    tmp_path: Path,
) -> None:
    inherited = {
        "PATH": "bin",
        "HTTP_PROXY": "http://wrong:1",
        "https_proxy": "http://wrong:2",
        "ALL_PROXY": "socks5://wrong:3",
        "PYTHONPATH": "existing",
    }
    assert proxy_process_environment(None, base=inherited) == {
        "PATH": "bin",
        "PYTHONPATH": "existing",
        "PIP_CONFIG_FILE": os.devnull,
        "NO_PROXY": "*",
        "no_proxy": "*",
    }

    proxy = build_download_proxy(
        url="socks5://127.0.0.1:7897",
        username="alice",
        password="secret",
    )
    module_directory = stage_pysocks_module(tmp_path / "proxy-bootstrap")
    env = proxy_process_environment(
        proxy,
        base=inherited,
        socks_module_directory=module_directory,
    )
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "PIP_PROXY",
    ):
        assert env[key] == proxy.transport_url
    assert env["PYTHONPATH"] == f"{module_directory}{os.pathsep}existing"
    staged_module = (module_directory / "socks.py").read_bytes()
    installed_module = Path(proxy_module.socks.__file__).read_bytes()
    assert staged_module == installed_module
    assert str(Path(proxy_module.socks.__file__).parent) not in env["PYTHONPATH"]
    assert env["PIP_CONFIG_FILE"] == os.devnull
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"


def test_proxy_redaction_never_returns_credentials() -> None:
    proxy = build_download_proxy(
        url="http://127.0.0.1:7897",
        username="alice",
        password="top-secret",
    )
    basic_token = base64.b64encode(b"alice:top-secret").decode("ascii")
    message = (
        f"failed via {proxy.transport_url} and socks5://bob:hidden@127.0.0.1:8 "
        f"with Proxy-Authorization: Basic {basic_token}"
    )
    redacted = redact_proxy_credentials(message, proxy)
    assert "top-secret" not in redacted
    assert "hidden" not in redacted
    assert basic_token not in redacted
    assert "***@" in redacted or "••••" in redacted

    complex_proxy = build_download_proxy(
        url="https://127.0.0.1:7898",
        username="name@example.com",
        password="p@ss word",
    )
    decoded_message = (
        "failed via https://name@example.com:p@ss word@127.0.0.1:7898 "
        "and http://other:raw-secret@127.0.0.1:9"
    )
    decoded_redacted = redact_proxy_credentials(decoded_message, complex_proxy)
    assert "name@example.com" not in decoded_redacted
    assert "p@ss word" not in decoded_redacted
    assert "raw-secret" not in decoded_redacted


def test_socks_proxy_rejects_credentials_larger_than_protocol_fields() -> None:
    with pytest.raises(ProxyConfigurationError, match="SOCKS5"):
        build_download_proxy(
            url="socks5://127.0.0.1:1080",
            username="用" * 86,
            password="secret",
        )


def test_terminal_ai_jobs_forget_proxy_credentials_and_redact_errors(
    monkeypatch,
    ffmpeg: str,
    ffprobe: str,
    tmp_path: Path,
) -> None:
    proxy = build_download_proxy(
        url="http://127.0.0.1:7897",
        username="alice",
        password="top-secret",
    )
    basic_token = base64.b64encode(b"alice:top-secret").decode("ascii")
    private_error = f"failed via {proxy.transport_url}; Basic {basic_token}"
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )

    download_job = AIModelDownloadJob(id="download", download_proxy=proxy)
    manager._active_job_id = download_job.id

    def fail_runtime(_job) -> None:
        raise MediaError(private_error)

    monkeypatch.setattr(manager, "_prepare_runtime", fail_runtime)
    manager._run_model_download(download_job)

    assert download_job.status == "failed"
    assert download_job.download_proxy is None
    assert manager._active_job_id is None
    assert "top-secret" not in str(download_job.error)
    assert basic_token not in str(download_job.error)

    source = VideoSource(id="source", path=tmp_path / "source.mp4", metadata={})
    enhance_job = AIEnhancementJob(
        id="enhance",
        source=source,
        target=AIResolutionTarget("1080p", "1080p", 1080, 1920),
        output_path=tmp_path / "output.mp4",
        expected_width=1920,
        expected_height=1080,
        download_proxy=proxy,
    )
    manager._active_job_id = enhance_job.id
    monkeypatch.setattr(manager, "_audit_frame_timing", fail_runtime)
    manager._run(enhance_job)

    assert enhance_job.status == "failed"
    assert enhance_job.download_proxy is None
    assert manager._active_job_id is None
    assert "top-secret" not in str(enhance_job.error)
    assert basic_token not in str(enhance_job.error)


def test_ai_model_job_captures_proxy_once(
    monkeypatch,
    ffmpeg: str,
    ffprobe: str,
    tmp_path: Path,
) -> None:
    first = build_download_proxy(url="http://127.0.0.1:7897")
    selected = first
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        download_proxy_provider=lambda: selected,
    )
    monkeypatch.setattr(manager, "encoder_available", True)
    monkeypatch.setattr(manager, "color_pipeline_available", True)
    monkeypatch.setattr(manager, "_runtime_installed", lambda _model_id=None: False)
    monkeypatch.setattr(manager, "_models_downloaded", lambda _model_id=None: False)
    monkeypatch.setattr(manager, "_available_model_bytes", lambda _model_id=None: 0)
    monkeypatch.setattr("app.ai_enhance.threading.Thread.start", lambda _thread: None)

    snapshot = manager.start_model_download()
    job = manager._model_download_jobs[snapshot["job_id"]]
    selected = build_download_proxy(url="http://127.0.0.1:7898")

    assert job.download_proxy == first
    assert manager._download_proxy_snapshot() == selected


def test_proxy_api_persists_masks_tests_and_clears_without_losing_output_directory(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    state.set_output_directory(tmp_path)
    tested: list[str] = []

    def fake_test(proxy):
        tested.append(proxy.transport_url)
        return 42, "代理连接成功，可以用于下载 AI 模型。"

    monkeypatch.setattr(main_module, "test_download_proxy", fake_test)
    sentinel = "never-show-this-password"
    with TestClient(create_app(state)) as client:
        token = client.get("/api/bootstrap").json()["app_token"]
        headers = {"X-App-Token": token}
        assert client.get("/api/ai-download-proxy").status_code == 403

        saved = client.put(
            "/api/ai-download-proxy",
            headers=headers,
            json={
                "url": "socks5h://127.0.0.1:7897/",
                "username": "alice",
                "password": sentinel,
                "password_action": "replace",
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert saved.json()["display_url"] == "socks5h://al***:••••@127.0.0.1:7897"
        assert sentinel not in saved.text
        assert sentinel not in client.get("/api/bootstrap").text
        assert sentinel not in client.get("/api/ai-models", headers=headers).text

        checked = client.post(
            "/api/ai-download-proxy/test",
            headers=headers,
            json={
                "url": "socks5h://127.0.0.1:7897",
                "username": "alice",
                "password": "",
                "password_action": "keep",
            },
        )
        assert checked.status_code == 200, checked.text
        assert checked.json()["latency_ms"] == 42
        assert tested == ["socks5h://alice:never-show-this-password@127.0.0.1:7897"]
        assert sentinel not in checked.text

        cleared = client.delete("/api/ai-download-proxy", headers=headers)
        assert cleared.status_code == 200
        assert cleared.json()["configured"] is False
        assert state.output_directory() == tmp_path.resolve()


def test_proxy_api_validation_error_does_not_echo_password(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    sentinel = "do-not-echo-this-secret"
    with TestClient(create_app(state)) as client:
        token = client.get("/api/bootstrap").json()["app_token"]
        response = client.put(
            "/api/ai-download-proxy",
            headers={"X-App-Token": token},
            json={
                "url": "socks5://127.0.0.1",
                "username": "alice",
                "password": sentinel,
                "password_action": "replace",
            },
        )
        assert response.status_code == 400
        assert sentinel not in response.text
