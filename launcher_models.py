from __future__ import annotations

import json
import math
import queue
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

HTTP_TIMEOUT_SECONDS = 15
POLL_INTERVAL_SECONDS = 0.25
MAX_STATUS_POLL_FAILURES = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ACTIVE_DOWNLOAD_STATUSES = frozenset({"queued", "running"})


class ModelPromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class StartupModel:
    id: str
    name: str
    download_size_bytes: int
    partial_bytes: int


def _write(stream: TextIO, value: str) -> None:
    try:
        stream.write(value)
        stream.flush()
    except (OSError, ValueError):
        pass


def _request_json(
    port: int,
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    }
    data: bytes | None = None
    if token is not None:
        headers["X-App-Token"] = token
    if method == "POST":
        data = b"{}"
        headers["Content-Type"] = "application/json"
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise ModelPromptError(f"local API request failed: {exc}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ModelPromptError("local API response was too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelPromptError("local API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ModelPromptError("local API returned an unexpected response")
    return payload


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, result)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _clean_terminal_text(value: object, *, fallback: str) -> str:
    text = str(value or fallback)
    cleaned = "".join(character for character in text if character >= " " and character != "\x7f")
    return cleaned.strip()[:300] or fallback


def _startup_models(payload: Mapping[str, Any]) -> list[StartupModel]:
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise ModelPromptError("local API did not return an AI model list")
    models: list[StartupModel] = []
    seen: set[str] = set()
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            continue
        model_id = str(raw_model.get("id") or "").strip()
        if (
            not model_id
            or model_id in seen
            or raw_model.get("compatible") is not True
            or raw_model.get("prepared") is True
            or raw_model.get("startup_prompt") is not True
        ):
            continue
        seen.add(model_id)
        models.append(
            StartupModel(
                id=model_id,
                name=_clean_terminal_text(raw_model.get("name"), fallback=model_id),
                download_size_bytes=_nonnegative_integer(raw_model.get("download_size_bytes")),
                partial_bytes=_nonnegative_integer(raw_model.get("partial_bytes")),
            )
        )
    return models


def _format_bytes(value: float | int) -> str:
    size = max(0.0, float(value))
    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def _format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    remaining = max(0, int(math.ceil(seconds)))
    days, remainder = divmod(remaining, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds_part = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m {seconds_part:02d}s"
    if minutes:
        return f"{minutes}m {seconds_part:02d}s"
    return f"{seconds_part}s"


def _progress_line(snapshot: Mapping[str, Any], model: StartupModel) -> str:
    status = str(snapshot.get("status") or "queued").lower()
    stage = _clean_terminal_text(snapshot.get("stage"), fallback=status).title()
    downloaded = _nonnegative_integer(snapshot.get("downloaded_bytes", model.partial_bytes))
    total = _nonnegative_integer(snapshot.get("total_bytes", model.download_size_bytes))
    progress = _finite_number(snapshot.get("progress"))
    if progress is None:
        progress = downloaded / total * 100 if total else 0.0
    progress = min(100.0, max(0.0, progress))
    speed = _finite_number(snapshot.get("download_speed_bps"))
    speed_text = f"{_format_bytes(speed)}/s" if speed is not None and speed > 0 else "--"
    eta = _finite_number(snapshot.get("estimated_remaining_seconds"))
    amount = f"{_format_bytes(downloaded)} / {_format_bytes(total)}"
    return f"  {stage} {progress:.1f}% | {amount} | {speed_text} | ETA {_format_eta(eta)}"


class _ProgressOutput:
    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        try:
            self.tty = bool(stream.isatty())
        except (AttributeError, OSError):
            self.tty = False
        self._line_width = 0
        self._has_tty_line = False
        self._last_log_key: tuple[str, str, int] | None = None

    def update(self, snapshot: Mapping[str, Any], model: StartupModel) -> None:
        line = _progress_line(snapshot, model)
        if self.tty:
            padded = line.ljust(self._line_width)
            self._line_width = max(self._line_width, len(line))
            self._has_tty_line = True
            _write(self.stream, f"\r{padded}")
            return
        progress = _finite_number(snapshot.get("progress")) or 0.0
        key = (
            str(snapshot.get("status") or ""),
            str(snapshot.get("stage") or ""),
            min(20, max(0, int(progress // 5))),
        )
        if key != self._last_log_key:
            self._last_log_key = key
            _write(self.stream, f"{line}\n")

    def finish(self) -> None:
        if self.tty and self._has_tty_line:
            _write(self.stream, "\n")
            self._has_tty_line = False


def _model_path(model_id: str, suffix: str = "") -> str:
    return f"/api/ai-models/{quote(model_id, safe='')}/download{suffix}"


def _attempt_cancel(port: int, token: str, model_id: str, output: TextIO) -> bool:
    try:
        _request_json(
            port,
            _model_path(model_id, "/cancel"),
            method="POST",
            token=token,
        )
        _write(output, "AI model cancellation was requested; partial data was kept.\n")
        return True
    except (Exception, KeyboardInterrupt) as exc:
        _write(output, f"AI model cancellation request failed: {exc}\n")
        return False


def _wait_for_download(
    port: int,
    token: str,
    model: StartupModel,
    initial: Mapping[str, Any],
    stop_requested: threading.Event,
    output: TextIO,
) -> str:
    progress_output = _ProgressOutput(output)
    snapshot: Mapping[str, Any] = initial
    consecutive_poll_failures = 0
    if not snapshot.get("status"):
        snapshot = {**snapshot, "status": "queued", "stage": "queued"}
    try:
        while True:
            progress_output.update(snapshot, model)
            status = str(snapshot.get("status") or "").lower()
            if status not in ACTIVE_DOWNLOAD_STATUSES:
                break
            if stop_requested.is_set():
                progress_output.finish()
                _attempt_cancel(port, token, model.id, output)
                return "stopped"
            if stop_requested.wait(POLL_INTERVAL_SECONDS):
                progress_output.finish()
                _attempt_cancel(port, token, model.id, output)
                return "stopped"
            try:
                snapshot = _request_json(port, _model_path(model.id), token=token)
            except ModelPromptError as exc:
                consecutive_poll_failures += 1
                if consecutive_poll_failures >= MAX_STATUS_POLL_FAILURES:
                    raise
                _write(
                    output,
                    "\nAI model status check failed; "
                    f"retrying ({consecutive_poll_failures}/{MAX_STATUS_POLL_FAILURES}) "
                    f"after: {exc}\n",
                )
                continue
            consecutive_poll_failures = 0
    finally:
        progress_output.finish()

    status = str(snapshot.get("status") or "unknown").lower()
    if status == "completed":
        _write(output, f"{model.name} is ready.\n")
    else:
        detail = _clean_terminal_text(
            snapshot.get("error") or snapshot.get("message"),
            fallback=f"status: {status}",
        )
        _write(output, f"{model.name} was not prepared ({detail}); startup will continue.\n")
    return status


def _readline_until_stopped(
    input_stream: TextIO,
    stop_requested: threading.Event,
) -> str | None:
    result: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            result.put(input_stream.readline())
        except BaseException as exc:
            result.put(exc)

    threading.Thread(target=read, name="ai-model-prompt-input", daemon=True).start()
    while not stop_requested.wait(0.1):
        try:
            value = result.get_nowait()
        except queue.Empty:
            continue
        if isinstance(value, BaseException):
            if isinstance(value, (EOFError, KeyboardInterrupt)):
                raise value
            raise ModelPromptError(f"terminal input failed: {value}") from value
        return value
    return None


def _read_download_choice(
    input_stream: TextIO,
    output_stream: TextIO,
    stop_requested: threading.Event,
) -> bool | None:
    while True:
        _write(output_stream, "Download this model now? [y/N]: ")
        try:
            answer = _readline_until_stopped(input_stream, stop_requested)
        except EOFError:
            return None
        if answer is None or answer == "":
            return None
        normalized = answer.strip().lower()
        if normalized in {"y", "yes"}:
            return True
        if normalized in {"", "n", "no"}:
            return False
        _write(output_stream, "Please enter y or n.\n")


def prompt_for_missing_models(
    port: int,
    stop_requested: threading.Event,
    *,
    skip: bool = False,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> None:
    """Offer optional model downloads before the launcher opens the browser.

    Explicit streams are intended for embedding and tests. With the default stdin,
    a non-interactive launch skips all prompts so automation cannot be blocked.
    """

    if skip or stop_requested.is_set():
        return
    source = input_stream if input_stream is not None else sys.stdin
    output = output_stream if output_stream is not None else sys.stdout
    if input_stream is None:
        try:
            if not source.isatty():
                return
        except (AttributeError, OSError):
            return

    active_model_id: str | None = None
    token: str | None = None
    try:
        bootstrap = _request_json(port, "/api/bootstrap")
        token_value = bootstrap.get("app_token")
        if not isinstance(token_value, str) or not token_value:
            raise ModelPromptError("local API did not provide an application token")
        token = token_value
        models = _startup_models(_request_json(port, "/api/ai-models", token=token))
        if not models:
            return

        _write(output, "Optional AI models can be downloaded before the app opens.\n")
        for index, model in enumerate(models, start=1):
            if stop_requested.is_set():
                return
            size = _format_bytes(model.download_size_bytes)
            resume = (
                f", {_format_bytes(model.partial_bytes)} already downloaded"
                if model.partial_bytes
                else ""
            )
            _write(output, f"[{index}/{len(models)}] {model.name} ({size}{resume})\n")
            choice = _read_download_choice(source, output, stop_requested)
            if choice is None:
                if stop_requested.is_set():
                    return
                _write(output, "Input closed; remaining AI model downloads were skipped.\n")
                return
            if not choice:
                _write(output, f"Skipped {model.name}.\n")
                continue

            active_model_id = model.id
            try:
                initial = _request_json(
                    port,
                    _model_path(model.id),
                    method="POST",
                    token=token,
                )
                outcome = _wait_for_download(
                    port,
                    token,
                    model,
                    initial,
                    stop_requested,
                    output,
                )
            except KeyboardInterrupt:
                _attempt_cancel(port, token, model.id, output)
                active_model_id = None
                _write(output, "AI model setup was interrupted; startup will continue.\n")
                return
            except Exception as exc:
                cancellation_requested = _attempt_cancel(port, token, model.id, output)
                _write(
                    output,
                    (
                        f"{model.name} download monitoring stopped ({exc}). "
                        "Partial data was kept; confirm its status in Model Manager after "
                        "the browser opens.\n"
                        if cancellation_requested
                        else f"{model.name} download status is uncertain ({exc}). "
                        "Check Model Manager after the browser opens.\n"
                    ),
                )
                active_model_id = None
                return
            active_model_id = None
            if outcome == "stopped":
                return
    except KeyboardInterrupt:
        if active_model_id is not None and token is not None:
            _attempt_cancel(port, token, active_model_id, output)
        _write(output, "AI model setup was interrupted; startup will continue.\n")
    except Exception as exc:
        _write(output, f"AI model check was skipped ({exc}); startup will continue.\n")


__all__ = ["prompt_for_missing_models"]
