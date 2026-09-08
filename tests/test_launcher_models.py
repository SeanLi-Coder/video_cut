from __future__ import annotations

import io
import json
import threading
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

import launcher_models


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class FakeAPI:
    def __init__(
        self,
        responses: list[
            tuple[
                str,
                str,
                dict[str, Any] | BaseException | Callable[[Request], dict[str, Any]],
            ]
        ],
    ) -> None:
        self.responses = list(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: float) -> FakeResponse:
        assert timeout == launcher_models.HTTP_TIMEOUT_SECONDS
        self.requests.append(request)
        assert self.responses, f"unexpected request: {request.get_method()} {request.full_url}"
        expected_method, expected_path, result = self.responses.pop(0)
        assert request.get_method() == expected_method
        assert urlsplit(request.full_url).path == expected_path
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            result = result(request)
        return FakeResponse(json.dumps(result).encode("utf-8"))

    def assert_finished(self) -> None:
        assert self.responses == []


class TTYStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class BlockingInput:
    def __init__(self) -> None:
        self.read_started = threading.Event()
        self.release = threading.Event()

    def readline(self) -> str:
        self.read_started.set()
        self.release.wait()
        return ""


def _bootstrap() -> dict[str, str]:
    return {"app_token": "test-token"}


def _model(
    model_id: str = "seedvr2-3b",
    *,
    name: str = "SeedVR2 3B FP16",
    compatible: bool = True,
    downloaded: bool = False,
    prepared: bool = False,
    startup_prompt: bool = True,
    download_size_bytes: int = 2 * 1024 * 1024,
    partial_bytes: int = 0,
    download_status: str = "idle",
    requires_runtime_update: bool = False,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": name,
        "compatible": compatible,
        "downloaded": downloaded,
        "prepared": prepared,
        "startup_prompt": startup_prompt,
        "download_size_bytes": download_size_bytes,
        "partial_bytes": partial_bytes,
        "download_status": download_status,
        "requires_runtime_update": requires_runtime_update,
    }


def _headers(request: Request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.header_items()}


def test_skip_avoids_all_api_and_terminal_access(monkeypatch) -> None:
    def unexpected_request(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the API must not be contacted")

    monkeypatch.setattr(launcher_models, "urlopen", unexpected_request)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        skip=True,
        input_stream=io.StringIO("y\n"),
        output_stream=output,
    )

    assert output.getvalue() == ""


def test_default_non_tty_input_never_blocks_automation(monkeypatch) -> None:
    def unexpected_request(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the API must not be contacted")

    monkeypatch.setattr(launcher_models, "urlopen", unexpected_request)
    monkeypatch.setattr(launcher_models.sys, "stdin", io.StringIO("y\n"))
    output = io.StringIO()
    monkeypatch.setattr(launcher_models.sys, "stdout", output)

    launcher_models.prompt_for_missing_models(8777, threading.Event())

    assert output.getvalue() == ""


def test_eof_skips_remaining_models_and_returns_normally(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            ("GET", "/api/ai-models", {"models": [_model()]}),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO(""),
        output_stream=output,
    )

    api.assert_finished()
    assert "Input closed" in output.getvalue()
    assert not any(request.get_method() == "POST" for request in api.requests)


def test_only_compatible_missing_startup_models_are_prompted(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model("missing-one", name="Missing One", partial_bytes=1024),
                        _model("installed", name="Installed", downloaded=True, prepared=True),
                        _model("incompatible", name="Incompatible", compatible=False),
                        _model("manual", name="Manual Only", startup_prompt=False),
                        _model("missing-two", name="Missing Two"),
                        _model("missing-one", name="Duplicate"),
                        {"name": "No identifier", "compatible": True, "startup_prompt": True},
                    ]
                },
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("n\n\n"),
        output_stream=output,
    )

    api.assert_finished()
    text = output.getvalue()
    assert "[1/2] Missing One" in text
    assert "1.0 KB already downloaded" in text
    assert "[2/2] Missing Two" in text
    assert "Installed" not in text
    assert "Incompatible" not in text
    assert "Manual Only" not in text
    assert text.count("Continue, restart from zero, or skip?") == 1
    assert text.count("Download this model now?") == 1
    assert not any(request.get_method() == "POST" for request in api.requests)


def test_downloaded_model_with_stale_runtime_is_still_prompted(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model(
                            "stale-runtime",
                            name="Stale Runtime",
                            downloaded=True,
                            prepared=False,
                        )
                    ]
                },
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("n\n"),
        output_stream=output,
    )

    api.assert_finished()
    assert "[1/1] Stale Runtime" in output.getvalue()
    assert "Model files are complete, but the AI runtime needs repair." in output.getvalue()
    assert "Skipped Stale Runtime" in output.getvalue()


def test_partial_download_can_continue_from_saved_data(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model(
                            "resume-me",
                            name="Resume Me",
                            partial_bytes=1024,
                            download_status="cancelled",
                        )
                    ]
                },
            ),
            (
                "POST",
                "/api/ai-models/resume-me/download",
                {"status": "completed", "stage": "completed", "progress": 100},
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("c\n"),
        output_stream=output,
    )

    api.assert_finished()
    text = output.getvalue()
    assert "A previous model download is incomplete or failed." in text
    assert "Continue, restart from zero, or skip? [c/r/N]:" in text
    assert "Resume Me is ready." in text


def test_partial_download_can_be_removed_and_restarted(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model(
                            "restart/model",
                            name="Restart Me",
                            partial_bytes=1024,
                            download_status="failed",
                        )
                    ]
                },
            ),
            (
                "POST",
                "/api/ai-models/restart%2Fmodel/download/restart",
                {"status": "completed", "stage": "completed", "progress": 100},
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("invalid\nrestart\n"),
        output_stream=output,
    )

    api.assert_finished()
    assert "Please enter c, r, or n." in output.getvalue()
    assert "Restart Me is ready." in output.getvalue()


def test_recovery_only_ignores_models_that_were_never_started(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model("fresh", name="Fresh Model"),
                        _model(
                            "failed",
                            name="Failed Model",
                            download_status="failed",
                        ),
                    ]
                },
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        recovery_only=True,
        input_stream=io.StringIO("n\n"),
        output_stream=output,
    )

    api.assert_finished()
    text = output.getvalue()
    assert "Failed Model" in text
    assert "Fresh Model" not in text


def test_running_web_download_is_attached_without_prompting(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model(
                            "running",
                            name="Running Model",
                            partial_bytes=1024,
                            download_status="running",
                        )
                    ]
                },
            ),
            (
                "GET",
                "/api/ai-models/running/download",
                {"status": "completed", "stage": "completed", "progress": 100},
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        recovery_only=True,
        input_stream=io.StringIO(""),
        output_stream=output,
    )

    api.assert_finished()
    text = output.getvalue()
    assert "A download is already running; showing its progress here." in text
    assert "Download this model now?" not in text
    assert "Continue, restart from zero, or skip?" not in text
    assert "Running Model is ready." in text


def test_attached_web_download_is_not_cancelled_when_status_checks_fail(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model(
                            "web-download",
                            name="Web Download",
                            partial_bytes=1024,
                            download_status="running",
                        )
                    ]
                },
            ),
            (
                "GET",
                "/api/ai-models/web-download/download",
                {"status": "running", "stage": "download", "progress": 10},
            ),
            ("GET", "/api/ai-models/web-download/download", URLError("temporary failure")),
            ("GET", "/api/ai-models/web-download/download", URLError("temporary failure")),
            ("GET", "/api/ai-models/web-download/download", URLError("server closed")),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    monkeypatch.setattr(launcher_models, "POLL_INTERVAL_SECONDS", 0)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        recovery_only=True,
        input_stream=io.StringIO(""),
        output_stream=output,
    )

    api.assert_finished()
    assert not any(request.get_method() == "POST" for request in api.requests)
    assert "download status is uncertain" in output.getvalue()


def test_attached_web_download_is_not_cancelled_on_keyboard_interrupt(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model(
                            "web-download",
                            partial_bytes=1024,
                            download_status="running",
                        )
                    ]
                },
            ),
            (
                "GET",
                "/api/ai-models/web-download/download",
                {"status": "running", "stage": "download", "progress": 10},
            ),
            ("GET", "/api/ai-models/web-download/download", KeyboardInterrupt()),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    monkeypatch.setattr(launcher_models, "POLL_INTERVAL_SECONDS", 0)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        recovery_only=True,
        input_stream=io.StringIO(""),
        output_stream=output,
    )

    api.assert_finished()
    assert not any(request.get_method() == "POST" for request in api.requests)
    assert "setup was interrupted; startup will continue" in output.getvalue()


def test_attached_web_download_is_not_cancelled_on_stop(monkeypatch) -> None:
    stop_requested = threading.Event()

    def attach_download(_request: Request) -> dict[str, Any]:
        stop_requested.set()
        return {"status": "running", "stage": "download", "progress": 10}

    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model(
                            "web-download",
                            partial_bytes=1024,
                            download_status="running",
                        )
                    ]
                },
            ),
            ("GET", "/api/ai-models/web-download/download", attach_download),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        stop_requested,
        recovery_only=True,
        input_stream=io.StringIO(""),
        output_stream=output,
    )

    api.assert_finished()
    assert not any(request.get_method() == "POST" for request in api.requests)
    assert "the existing download continues" in output.getvalue()


def test_failed_start_request_does_not_issue_an_unscoped_cancel(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            ("GET", "/api/ai-models", {"models": [_model("start-fails")]}),
            ("POST", "/api/ai-models/start-fails/download", URLError("start failed")),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("y\n"),
        output_stream=output,
    )

    api.assert_finished()
    assert [request.get_method() for request in api.requests].count("POST") == 1
    assert not any(urlsplit(request.full_url).path.endswith("/cancel") for request in api.requests)
    assert "download status is uncertain" in output.getvalue()


def test_restart_http_error_surfaces_safe_api_detail_without_cancelling(monkeypatch) -> None:
    error_body = io.BytesIO(json.dumps({"detail": "磁盘空间不足，请清理后重试。"}).encode("utf-8"))
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model(
                            "restart-fails",
                            partial_bytes=1024,
                            download_status="failed",
                        )
                    ]
                },
            ),
            (
                "POST",
                "/api/ai-models/restart-fails/download/restart",
                HTTPError(
                    "http://127.0.0.1:8777/api/ai-models/restart-fails/download/restart",
                    400,
                    "Bad Request",
                    hdrs=None,
                    fp=error_body,
                ),
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("r\n"),
        output_stream=output,
    )

    api.assert_finished()
    assert "磁盘空间不足，请清理后重试。" in output.getvalue()
    assert not any(urlsplit(request.full_url).path.endswith("/cancel") for request in api.requests)


def test_already_running_start_response_is_not_owned_or_cancelled(monkeypatch) -> None:
    stop_requested = threading.Event()

    def existing_download(_request: Request) -> dict[str, Any]:
        stop_requested.set()
        return {
            "job_id": "web-owned-job",
            "already_running": True,
            "status": "running",
            "stage": "download",
            "progress": 10,
        }

    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            ("GET", "/api/ai-models", {"models": [_model("race")]}),
            ("POST", "/api/ai-models/race/download", existing_download),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        stop_requested,
        input_stream=io.StringIO("y\n"),
        output_stream=output,
    )

    api.assert_finished()
    assert [request.get_method() for request in api.requests].count("POST") == 1
    assert "the existing download continues" in output.getvalue()


def test_invalid_start_job_id_is_not_used_for_cancellation(monkeypatch) -> None:
    stop_requested = threading.Event()

    def invalid_job(_request: Request) -> dict[str, Any]:
        stop_requested.set()
        return {
            "job_id": "unsafe/job/id",
            "status": "running",
            "stage": "download",
            "progress": 10,
        }

    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            ("GET", "/api/ai-models", {"models": [_model("invalid-job")]}),
            ("POST", "/api/ai-models/invalid-job/download", invalid_job),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        stop_requested,
        input_stream=io.StringIO("y\n"),
        output_stream=output,
    )

    api.assert_finished()
    assert [request.get_method() for request in api.requests].count("POST") == 1
    assert "the existing download continues" in output.getvalue()


def test_stop_event_interrupts_pending_terminal_input(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            ("GET", "/api/ai-models", {"models": [_model()]}),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    input_stream = BlockingInput()
    output = io.StringIO()
    stop_requested = threading.Event()
    worker = threading.Thread(
        target=launcher_models.prompt_for_missing_models,
        args=(8777, stop_requested),
        kwargs={"input_stream": input_stream, "output_stream": output},
    )
    worker.start()
    assert input_stream.read_started.wait(timeout=2)

    stop_requested.set()
    worker.join(timeout=2)
    input_stream.release.set()

    api.assert_finished()
    assert not worker.is_alive()
    assert "Input closed" not in output.getvalue()
    assert not any(request.get_method() == "POST" for request in api.requests)


def test_yes_downloads_and_renders_percentage_amount_speed_and_eta(monkeypatch) -> None:
    total = 2 * 1024 * 1024
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            ("GET", "/api/ai-models", {"models": [_model()]}),
            (
                "POST",
                "/api/ai-models/seedvr2-3b/download",
                {
                    "status": "queued",
                    "stage": "setup",
                    "progress": 0,
                    "downloaded_bytes": 0,
                    "total_bytes": total,
                    "download_speed_bps": 0,
                    "estimated_remaining_seconds": None,
                },
            ),
            (
                "GET",
                "/api/ai-models/seedvr2-3b/download",
                {
                    "status": "running",
                    "stage": "download",
                    "progress": 50,
                    "downloaded_bytes": 1024 * 1024,
                    "total_bytes": total,
                    "download_speed_bps": 1024 * 1024,
                    "estimated_remaining_seconds": 61,
                },
            ),
            (
                "GET",
                "/api/ai-models/seedvr2-3b/download",
                {
                    "status": "completed",
                    "stage": "completed",
                    "progress": 100,
                    "downloaded_bytes": total,
                    "total_bytes": total,
                    "download_speed_bps": 0,
                    "estimated_remaining_seconds": 0,
                },
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    monkeypatch.setattr(launcher_models, "POLL_INTERVAL_SECONDS", 0)
    output = TTYStringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("y\n"),
        output_stream=output,
    )

    api.assert_finished()
    text = output.getvalue()
    assert "\r" in text
    assert "Download 50.0%" in text
    assert "1.0 MB / 2.0 MB" in text
    assert "1.0 MB/s" in text
    assert "ETA 1m 01s" in text
    assert "Completed 100.0%" in text
    assert "SeedVR2 3B FP16 is ready." in text
    for request in api.requests[1:]:
        assert _headers(request)["x-app-token"] == "test-token"
    assert api.requests[2].data == b"{}"


def test_invalid_answer_reprompts_and_model_id_is_url_encoded(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            ("GET", "/api/ai-models", {"models": [_model("quality/model")]}),
            (
                "POST",
                "/api/ai-models/quality%2Fmodel/download",
                {"status": "completed", "stage": "completed", "progress": 100},
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("maybe\nyes\n"),
        output_stream=output,
    )

    api.assert_finished()
    assert "Please enter y or n." in output.getvalue()
    assert output.getvalue().count("Download this model now?") == 2


def test_failed_download_does_not_prevent_later_prompts(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model("first", name="First Model"),
                        _model("second", name="Second Model"),
                    ]
                },
            ),
            (
                "POST",
                "/api/ai-models/first/download",
                {
                    "status": "failed",
                    "stage": "failed",
                    "progress": 37,
                    "error": "network unavailable",
                },
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("y\nn\n"),
        output_stream=output,
    )

    api.assert_finished()
    text = output.getvalue()
    assert "First Model was not prepared (network unavailable); startup will continue." in text
    assert "[2/2] Second Model" in text
    assert "Skipped Second Model." in text


def test_api_failure_is_reported_but_never_raised(monkeypatch) -> None:
    api = FakeAPI([("GET", "/api/bootstrap", URLError("server closed"))])
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("y\n"),
        output_stream=output,
    )

    api.assert_finished()
    assert "AI model check was skipped" in output.getvalue()
    assert "startup will continue" in output.getvalue()


def test_repeated_poll_failure_requests_cancel_and_stops_prompting(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            (
                "GET",
                "/api/ai-models",
                {
                    "models": [
                        _model("first", name="First Model"),
                        _model("second", name="Second Model"),
                    ]
                },
            ),
            (
                "POST",
                "/api/ai-models/first/download",
                {
                    "job_id": "first-job",
                    "already_running": False,
                    "status": "running",
                    "stage": "download",
                    "progress": 10,
                },
            ),
            ("GET", "/api/ai-models/first/download", URLError("temporary failure")),
            ("GET", "/api/ai-models/first/download", URLError("temporary failure")),
            ("GET", "/api/ai-models/first/download", URLError("server closed")),
            (
                "POST",
                "/api/ai-models/first/download/cancel",
                {"status": "running"},
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    monkeypatch.setattr(launcher_models, "POLL_INTERVAL_SECONDS", 0)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("y\nn\n"),
        output_stream=output,
    )

    api.assert_finished()
    text = output.getvalue()
    assert text.count("status check failed") == 2
    assert "cancellation was requested" in text
    assert "confirm its status in Model Manager" in text
    assert "[2/2] Second Model" not in text
    assert api.requests[-1].data == b'{"job_id":"first-job"}'


def test_keyboard_interrupt_during_poll_attempts_cancel(monkeypatch) -> None:
    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            ("GET", "/api/ai-models", {"models": [_model("interrupt-me")]}),
            (
                "POST",
                "/api/ai-models/interrupt-me/download",
                {
                    "job_id": "interrupt-job",
                    "already_running": False,
                    "status": "running",
                    "stage": "download",
                    "progress": 10,
                },
            ),
            (
                "GET",
                "/api/ai-models/interrupt-me/download",
                KeyboardInterrupt(),
            ),
            (
                "POST",
                "/api/ai-models/interrupt-me/download/cancel",
                {"status": "cancelled"},
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    monkeypatch.setattr(launcher_models, "POLL_INTERVAL_SECONDS", 0)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        threading.Event(),
        input_stream=io.StringIO("y\n"),
        output_stream=output,
    )

    api.assert_finished()
    text = output.getvalue()
    assert "cancellation was requested" in text
    assert "setup was interrupted; startup will continue" in text
    assert api.requests[-1].data == b'{"job_id":"interrupt-job"}'


def test_stop_event_during_download_attempts_cancel(monkeypatch) -> None:
    stop_requested = threading.Event()

    def start_download(_request: Request) -> dict[str, Any]:
        stop_requested.set()
        return {
            "job_id": "stop-job",
            "already_running": False,
            "status": "running",
            "stage": "download",
            "progress": 10,
        }

    api = FakeAPI(
        [
            ("GET", "/api/bootstrap", _bootstrap()),
            ("GET", "/api/ai-models", {"models": [_model("stop-me")]}),
            ("POST", "/api/ai-models/stop-me/download", start_download),
            (
                "POST",
                "/api/ai-models/stop-me/download/cancel",
                {"status": "cancelled"},
            ),
        ]
    )
    monkeypatch.setattr(launcher_models, "urlopen", api)
    output = io.StringIO()

    launcher_models.prompt_for_missing_models(
        8777,
        stop_requested,
        input_stream=io.StringIO("y\n"),
        output_stream=output,
    )

    api.assert_finished()
    assert "cancellation was requested" in output.getvalue()
    assert api.requests[-1].data == b'{"job_id":"stop-job"}'
