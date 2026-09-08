from __future__ import annotations

import contextlib
import getpass
import json
import math
import queue
import re
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TextIO, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

HTTP_TIMEOUT_SECONDS = 15
MODEL_CATALOG_TIMEOUT_SECONDS = 10 * 60
POLL_INTERVAL_SECONDS = 0.25
MAX_STATUS_POLL_FAILURES = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ACTIVE_DOWNLOAD_STATUSES = frozenset({"queued", "running"})
DOWNLOAD_PROXY_PATH = "/api/ai-download-proxy"

# Local control requests must never inherit HTTP(S)_PROXY or ALL_PROXY. Besides
# making startup unreliable, proxying loopback could disclose application tokens.
_DIRECT_PROXY_HANDLER = ProxyHandler({})
_DIRECT_LOCAL_OPENER = build_opener(_DIRECT_PROXY_HANDLER)
urlopen = _DIRECT_LOCAL_OPENER.open
_PROXY_USERINFO_PATTERN = re.compile(r"(?i)(https?|socks5h?)://[^\s/@]+(?::[^\s/@]*)?@")
_POSIX_TTY_UNAVAILABLE = object()


class ModelPromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class StartupModel:
    id: str
    name: str
    download_size_bytes: int
    partial_bytes: int
    downloaded: bool
    download_status: str
    requires_runtime_update: bool
    offline_managed: bool

    @property
    def active(self) -> bool:
        return self.download_status in ACTIVE_DOWNLOAD_STATUSES

    @property
    def recoverable(self) -> bool:
        return bool(
            self.active
            or self.requires_runtime_update
            or self.partial_bytes > 0
            or self.download_status in {"cancelled", "failed"}
        )


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
    json_body: Mapping[str, Any] | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    }
    data: bytes | None = None
    if token is not None:
        headers["X-App-Token"] = token
    if method in {"POST", "PUT"}:
        data = json.dumps(
            dict(json_body) if json_body is not None else {},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        detail = str(exc)
        try:
            error_raw = exc.read(MAX_RESPONSE_BYTES + 1)
            if len(error_raw) <= MAX_RESPONSE_BYTES:
                error_payload = json.loads(error_raw.decode("utf-8"))
                if isinstance(error_payload, dict) and isinstance(error_payload.get("detail"), str):
                    detail = _clean_terminal_text(error_payload["detail"], fallback=detail)
        except (AttributeError, TypeError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        raise ModelPromptError(f"local API request failed: {detail}") from exc
    except (URLError, OSError, TimeoutError) as exc:
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


def _valid_job_id(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    if not all(
        character.isascii() and (character.isalnum() or character in "-_.:") for character in value
    ):
        return None
    return value


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


def _startup_models(
    payload: Mapping[str, Any],
    *,
    recovery_only: bool = False,
) -> list[StartupModel]:
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
        downloaded = raw_model.get("downloaded") is True
        model = StartupModel(
            id=model_id,
            name=_clean_terminal_text(raw_model.get("name"), fallback=model_id),
            download_size_bytes=_nonnegative_integer(raw_model.get("download_size_bytes")),
            partial_bytes=_nonnegative_integer(raw_model.get("partial_bytes")),
            downloaded=downloaded,
            download_status=_clean_terminal_text(
                raw_model.get("download_status"),
                fallback="idle",
            ).lower(),
            requires_runtime_update=bool(
                raw_model.get("requires_runtime_update") is True
                or (downloaded and raw_model.get("prepared") is not True)
            ),
            offline_managed=raw_model.get("offline_managed") is True,
        )
        if recovery_only and not model.recoverable:
            continue
        models.append(model)
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


def _attempt_cancel(
    port: int,
    token: str,
    model_id: str,
    job_id: str,
    output: TextIO,
) -> bool:
    try:
        _request_json(
            port,
            _model_path(model_id, "/cancel"),
            method="POST",
            token=token,
            json_body={"job_id": job_id},
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
    *,
    owned_job_id: str | None,
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
                if owned_job_id is not None:
                    _attempt_cancel(port, token, model.id, owned_job_id, output)
                else:
                    _write(output, "Stopped monitoring; the existing download continues.\n")
                return "stopped"
            if stop_requested.wait(POLL_INTERVAL_SECONDS):
                progress_output.finish()
                if owned_job_id is not None:
                    _attempt_cancel(port, token, model.id, owned_job_id, output)
                else:
                    _write(output, "Stopped monitoring; the existing download continues.\n")
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


def _proxy_display(payload: Mapping[str, Any]) -> str:
    if payload.get("configured") is not True:
        return "direct connection (no proxy)"
    display = _clean_terminal_text(
        payload.get("display_url"),
        fallback="configured proxy (address hidden)",
    )
    return _PROXY_USERINFO_PATTERN.sub(r"\1://***@", display)


def _read_proxy_line(
    input_stream: TextIO,
    output_stream: TextIO,
    stop_requested: threading.Event,
    prompt: str,
) -> str | None:
    _write(output_stream, prompt)
    try:
        answer = _readline_until_stopped(input_stream, stop_requested)
    except EOFError:
        return None
    if answer is None or answer == "":
        return None
    return answer.rstrip("\r\n")


def _getpass_until_stopped(
    output_stream: TextIO,
    stop_requested: threading.Event,
    *,
    use_posix_tty: bool,
) -> str | None:
    if stop_requested.is_set():
        return None
    password = _getpass_from_posix_tty(
        output_stream,
        stop_requested,
        enabled=use_posix_tty,
    )
    if password is not _POSIX_TTY_UNAVAILABLE:
        return cast(str | None, password)

    result: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            result.put(
                getpass.getpass(
                    "Proxy password (hidden; Enter clears it): ",
                    stream=output_stream,
                )
            )
        except BaseException as exc:
            result.put(exc)

    threading.Thread(target=read, name="ai-proxy-password-input", daemon=True).start()
    while not stop_requested.wait(0.1):
        try:
            value = result.get_nowait()
        except queue.Empty:
            continue
        if isinstance(value, BaseException):
            if isinstance(value, (EOFError, KeyboardInterrupt)):
                raise value
            raise ModelPromptError("secure proxy password input failed") from value
        return value
    return None


def _getpass_from_posix_tty(
    output_stream: TextIO,
    stop_requested: threading.Event,
    *,
    enabled: bool,
) -> str | None | object:
    if not enabled or sys.platform == "win32":
        return _POSIX_TTY_UNAVAILABLE

    import os
    import select
    import termios

    try:
        descriptor = os.open(
            "/dev/tty",
            os.O_RDWR | getattr(os, "O_NOCTTY", 0),
        )
    except OSError:
        return _POSIX_TTY_UNAVAILABLE

    original_attributes: list[Any] | None = None
    prompt_written = False
    try:
        original_attributes = termios.tcgetattr(descriptor)
        hidden_attributes = list(original_attributes)
        hidden_attributes[3] &= ~termios.ECHO
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, hidden_attributes)
        _write(output_stream, "Proxy password (hidden; Enter clears it): ")
        prompt_written = True
        while not stop_requested.is_set():
            try:
                readable, _, _ = select.select([descriptor], [], [], 0.1)
            except InterruptedError:
                continue
            if not readable:
                continue
            value = os.read(descriptor, 65_536)
            if not value:
                raise EOFError
            line = value.split(b"\n", 1)[0].rstrip(b"\r")
            encoding = os.device_encoding(descriptor) or "utf-8"
            return line.decode(encoding)
        return None
    except (EOFError, KeyboardInterrupt):
        raise
    except BaseException as exc:
        raise ModelPromptError("secure proxy password input failed") from exc
    finally:
        if original_attributes is not None:
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(descriptor, termios.TCSADRAIN, original_attributes)
        with contextlib.suppress(OSError):
            os.close(descriptor)
        if prompt_written:
            _write(output_stream, "\n")


def _test_saved_download_proxy(
    port: int,
    token: str,
    output: TextIO,
) -> None:
    try:
        result = _request_json(
            port,
            f"{DOWNLOAD_PROXY_PATH}/test",
            method="POST",
            token=token,
        )
        if result.get("test_success") is not True:
            raise ModelPromptError("local API did not confirm proxy connectivity")
        latency = _finite_number(result.get("latency_ms"))
        if latency is None or latency < 0:
            _write(output, "Proxy connection test succeeded.\n")
        else:
            _write(output, f"Proxy connection test succeeded ({latency:.0f} ms).\n")
    except ModelPromptError as exc:
        _write(
            output,
            "Proxy setting was saved, but its connection test failed "
            f"({exc}); model selection will continue.\n",
        )


def _prompt_for_download_proxy(
    port: int,
    token: str,
    input_stream: TextIO,
    output_stream: TextIO,
    stop_requested: threading.Event,
) -> bool:
    try:
        current = _request_json(port, DOWNLOAD_PROXY_PATH, token=token)
    except ModelPromptError as exc:
        _write(
            output_stream,
            f"AI download proxy settings are unavailable ({exc}); model selection will continue.\n",
        )
        return True

    _write(output_stream, f"Current AI download proxy: {_proxy_display(current)}\n")
    while True:
        action = _read_proxy_line(
            input_stream,
            output_stream,
            stop_requested,
            "AI download proxy [Enter=keep, s=set/change, c=clear]: ",
        )
        if action is None:
            return False
        normalized = action.strip().lower()
        if normalized == "":
            return True
        if normalized == "c":
            try:
                _request_json(
                    port,
                    DOWNLOAD_PROXY_PATH,
                    method="DELETE",
                    token=token,
                )
                _write(output_stream, "AI download proxy was cleared.\n")
            except ModelPromptError as exc:
                _write(
                    output_stream,
                    f"AI download proxy could not be cleared ({exc}); "
                    "model selection will continue.\n",
                )
            return True
        if normalized != "s":
            _write(output_stream, "Please press Enter, or enter s or c.\n")
            continue

        while True:
            proxy_url = _read_proxy_line(
                input_stream,
                output_stream,
                stop_requested,
                "Proxy URL (http://, https://, socks5://, or socks5h://): ",
            )
            if proxy_url is None:
                return False
            proxy_url = proxy_url.strip()
            if proxy_url:
                break
            _write(output_stream, "Proxy URL is required.\n")

        username = _read_proxy_line(
            input_stream,
            output_stream,
            stop_requested,
            "Proxy username (optional): ",
        )
        if username is None:
            return False
        if stop_requested.is_set():
            return False
        try:
            use_posix_tty = bool(input_stream.isatty())
        except (AttributeError, OSError):
            use_posix_tty = False
        try:
            password = _getpass_until_stopped(
                output_stream,
                stop_requested,
                use_posix_tty=use_posix_tty,
            )
        except EOFError:
            return False
        if password is None or stop_requested.is_set():
            return False

        request_body = {
            "url": proxy_url,
            "username": username.strip(),
            "password": password,
            "password_action": "replace" if password else "clear",
        }
        try:
            saved = _request_json(
                port,
                DOWNLOAD_PROXY_PATH,
                method="PUT",
                token=token,
                json_body=request_body,
            )
        except ModelPromptError as exc:
            _write(
                output_stream,
                f"AI download proxy could not be saved ({exc}); model selection will continue.\n",
            )
            return True
        _write(output_stream, f"AI download proxy saved: {_proxy_display(saved)}\n")
        _test_saved_download_proxy(port, token, output_stream)
        return True


def _read_download_choice(
    input_stream: TextIO,
    output_stream: TextIO,
    stop_requested: threading.Event,
    *,
    prompt: str = "Download this model now? [y/N]: ",
) -> bool | None:
    while True:
        _write(output_stream, prompt)
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


def _read_recovery_choice(
    input_stream: TextIO,
    output_stream: TextIO,
    stop_requested: threading.Event,
) -> str | None:
    while True:
        _write(output_stream, "Continue, restart from zero, or skip? [c/r/N]: ")
        try:
            answer = _readline_until_stopped(input_stream, stop_requested)
        except EOFError:
            return None
        if answer is None or answer == "":
            return None
        normalized = answer.strip().lower()
        if normalized in {"c", "continue", "y", "yes"}:
            return "continue"
        if normalized in {"r", "restart"}:
            return "restart"
        if normalized in {"", "n", "no"}:
            return "skip"
        _write(output_stream, "Please enter c, r, or n.\n")


def prompt_for_missing_models(
    port: int,
    stop_requested: threading.Event,
    *,
    skip: bool = False,
    recovery_only: bool = False,
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

    try:
        bootstrap = _request_json(port, "/api/bootstrap")
        token_value = bootstrap.get("app_token")
        if not isinstance(token_value, str) or not token_value:
            raise ModelPromptError("local API did not provide an application token")
        token = token_value
        _write(
            output,
            "Verifying local AI model files for this launch; an offline scan "
            "may take several minutes.\n",
        )
        models = _startup_models(
            _request_json(
                port,
                "/api/ai-models",
                token=token,
                timeout=MODEL_CATALOG_TIMEOUT_SECONDS,
            ),
            recovery_only=recovery_only,
        )
        if not models:
            return

        all_offline = all(model.offline_managed for model in models)
        _write(
            output,
            (
                "Recoverable AI model downloads were found.\n"
                if recovery_only
                else "Offline AI models can be verified and prepared before the app opens.\n"
                if all_offline
                else "Optional AI models can be downloaded before the app opens.\n"
            ),
        )
        needs_network_proxy = any(
            not model.active and not model.offline_managed for model in models
        )
        if needs_network_proxy and not _prompt_for_download_proxy(
            port,
            token,
            source,
            output,
            stop_requested,
        ):
            if not stop_requested.is_set():
                _write(
                    output,
                    "Input closed; remaining AI model downloads were skipped.\n",
                )
            return
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
            action = "download"
            if model.active:
                action = "attach"
                _write(output, "A download is already running; showing its progress here.\n")
            elif model.requires_runtime_update:
                _write(
                    output,
                    "Model files are complete, but the AI runtime needs repair.\n",
                )
                choice = _read_download_choice(
                    source,
                    output,
                    stop_requested,
                    prompt=(
                        "Prepare this offline model now? [y/N]: "
                        if model.offline_managed
                        else "Download this model now? [y/N]: "
                    ),
                )
                if choice is None:
                    if stop_requested.is_set():
                        return
                    _write(
                        output,
                        "Input closed; remaining AI model downloads were skipped.\n",
                    )
                    return
                if not choice:
                    _write(output, f"Skipped {model.name}.\n")
                    continue
            elif model.partial_bytes > 0 or model.download_status in {"cancelled", "failed"}:
                _write(output, "A previous model download is incomplete or failed.\n")
                recovery_choice = _read_recovery_choice(source, output, stop_requested)
                if recovery_choice is None:
                    if stop_requested.is_set():
                        return
                    _write(
                        output,
                        "Input closed; remaining AI model downloads were skipped.\n",
                    )
                    return
                if recovery_choice == "skip":
                    _write(output, f"Skipped {model.name}.\n")
                    continue
                action = recovery_choice
            else:
                choice = _read_download_choice(
                    source,
                    output,
                    stop_requested,
                    prompt=(
                        "Prepare this offline model now? [y/N]: "
                        if model.offline_managed
                        else "Download this model now? [y/N]: "
                    ),
                )
                if choice is None:
                    if stop_requested.is_set():
                        return
                    _write(
                        output,
                        "Input closed; remaining AI model downloads were skipped.\n",
                    )
                    return
                if not choice:
                    _write(output, f"Skipped {model.name}.\n")
                    continue

            owned_job_id: str | None = None
            try:
                initial = _request_json(
                    port,
                    _model_path(model.id, "/restart" if action == "restart" else ""),
                    method="GET" if action == "attach" else "POST",
                    token=token,
                )
                if action != "attach" and initial.get("already_running") is False:
                    owned_job_id = _valid_job_id(initial.get("job_id"))
                outcome = _wait_for_download(
                    port,
                    token,
                    model,
                    initial,
                    stop_requested,
                    output,
                    owned_job_id=owned_job_id,
                )
            except KeyboardInterrupt:
                if owned_job_id is not None:
                    _attempt_cancel(port, token, model.id, owned_job_id, output)
                _write(output, "AI model setup was interrupted; startup will continue.\n")
                return
            except Exception as exc:
                cancellation_requested = bool(
                    owned_job_id is not None
                    and _attempt_cancel(port, token, model.id, owned_job_id, output)
                )
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
                return
            if outcome == "stopped":
                return
    except KeyboardInterrupt:
        _write(output, "AI model setup was interrupted; startup will continue.\n")
    except Exception as exc:
        _write(output, f"AI model check was skipped ({exc}); startup will continue.\n")


__all__ = ["prompt_for_missing_models"]
