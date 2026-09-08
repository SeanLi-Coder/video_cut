from __future__ import annotations

import base64
import ipaddress
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import socks
import urllib3
from sockshandler import SocksiPyHandler

SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
PROXY_TEST_URL = "https://huggingface.co/robots.txt"
MAX_PROXY_URL_LENGTH = 2_048
MAX_PROXY_USERNAME_LENGTH = 256
MAX_PROXY_PASSWORD_LENGTH = 512
PROXY_ENVIRONMENT_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "PIP_PROXY",
    "NO_PROXY",
    "no_proxy",
)


class ProxyConfigurationError(ValueError):
    pass


class NetworkResponse(Protocol):
    status: int
    headers: Any

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> NetworkResponse: ...

    def __exit__(self, *args: object) -> object: ...


class _Urllib3NetworkResponse:
    def __init__(self, manager: urllib3.ProxyManager, response: urllib3.HTTPResponse) -> None:
        self._manager = manager
        self._response = response
        self.status = response.status
        self.headers = response.headers

    def read(self, amount: int = -1) -> bytes:
        try:
            return self._response.read(
                None if amount < 0 else amount,
                decode_content=False,
            )
        except Exception as exc:
            raise URLError("proxy response failed") from exc

    def __enter__(self) -> _Urllib3NetworkResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        try:
            self._response.close()
        finally:
            self._manager.clear()


def _contains_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _host_with_brackets(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _masked_username(username: str) -> str:
    if not username:
        return ""
    if len(username) == 1:
        return f"{username}***"
    return f"{username[:2]}***"


@dataclass(frozen=True)
class DownloadProxy:
    scheme: Literal["http", "https", "socks5", "socks5h"]
    host: str
    port: int
    username: str = ""
    password: str = field(default="", repr=False)

    @property
    def endpoint_url(self) -> str:
        return f"{self.scheme}://{_host_with_brackets(self.host)}:{self.port}"

    @property
    def transport_url(self) -> str:
        if not self.username:
            return self.endpoint_url
        credentials = quote(self.username, safe="")
        if self.password:
            credentials += f":{quote(self.password, safe='')}"
        return f"{self.scheme}://{credentials}@{_host_with_brackets(self.host)}:{self.port}"

    @property
    def display_url(self) -> str:
        if not self.username:
            return self.endpoint_url
        password_marker = ":••••" if self.password else ""
        return (
            f"{self.scheme}://{_masked_username(self.username)}{password_marker}"
            f"@{_host_with_brackets(self.host)}:{self.port}"
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "configured": True,
            "url": self.endpoint_url,
            "username": self.username,
            "has_password": bool(self.password),
            "display_url": self.display_url,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
        }

    def storage_payload(self) -> dict[str, str]:
        return {
            "url": self.endpoint_url,
            "username": self.username,
            "password": self.password,
        }


def direct_proxy_payload() -> dict[str, Any]:
    return {
        "configured": False,
        "url": "",
        "username": "",
        "has_password": False,
        "display_url": "直接连接（不使用系统代理）",
        "scheme": None,
        "host": None,
        "port": None,
    }


def parse_proxy_endpoint(value: object) -> tuple[str, str, int]:
    if not isinstance(value, str):
        raise ProxyConfigurationError("请输入代理地址。")
    text = value.strip()
    if not text:
        raise ProxyConfigurationError("请输入代理地址。")
    if len(text) > MAX_PROXY_URL_LENGTH or _contains_control_characters(text):
        raise ProxyConfigurationError("代理地址过长或包含无效字符。")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ProxyConfigurationError("代理端口无效，请输入 1 到 65535。") from exc
    scheme = parsed.scheme.lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ProxyConfigurationError("代理地址仅支持 http://、https://、socks5:// 或 socks5h://。")
    if parsed.username is not None or parsed.password is not None:
        raise ProxyConfigurationError("请把代理账号和密码填写在单独的输入框中。")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ProxyConfigurationError("代理地址只能包含协议、主机和端口。")
    host = parsed.hostname
    if (
        not host
        or _contains_control_characters(host)
        or any(character.isspace() or character in "%\\/@?#[]" for character in host)
    ):
        raise ProxyConfigurationError("代理地址缺少有效主机名。")
    try:
        normalized_host = (
            str(ipaddress.IPv6Address(host))
            if ":" in host
            else host.encode("idna").decode("ascii").lower()
        )
    except (UnicodeError, ValueError) as exc:
        raise ProxyConfigurationError("代理地址包含无效主机名。") from exc
    if len(normalized_host) > 253:
        raise ProxyConfigurationError("代理主机名过长。")
    if port is None or not 1 <= port <= 65_535:
        raise ProxyConfigurationError("代理地址必须包含端口，例如 :7897。")
    return scheme, normalized_host, port


def build_download_proxy(
    *,
    url: object,
    username: object = "",
    password: object = "",
) -> DownloadProxy:
    scheme, host, port = parse_proxy_endpoint(url)
    if not isinstance(username, str) or not isinstance(password, str):
        raise ProxyConfigurationError("代理账号或密码格式无效。")
    normalized_username = username.strip()
    if len(normalized_username) > MAX_PROXY_USERNAME_LENGTH:
        raise ProxyConfigurationError("代理账号过长。")
    if len(password) > MAX_PROXY_PASSWORD_LENGTH:
        raise ProxyConfigurationError("代理密码过长。")
    if _contains_control_characters(normalized_username) or _contains_control_characters(password):
        raise ProxyConfigurationError("代理账号或密码包含无效字符。")
    if password and not normalized_username:
        raise ProxyConfigurationError("填写代理密码前，请先填写代理账号。")
    try:
        username_bytes = normalized_username.encode("utf-8")
        password_bytes = password.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProxyConfigurationError("代理账号或密码包含无效字符。") from exc
    if scheme in {"socks5", "socks5h"} and (len(username_bytes) > 255 or len(password_bytes) > 255):
        raise ProxyConfigurationError("SOCKS5 代理账号或密码过长。")
    return DownloadProxy(
        scheme=cast(Literal["http", "https", "socks5", "socks5h"], scheme),
        host=host,
        port=port,
        username=normalized_username,
        password=password,
    )


def download_proxy_from_storage(value: object) -> DownloadProxy | None:
    if not isinstance(value, dict):
        return None
    try:
        return build_download_proxy(
            url=value.get("url"),
            username=value.get("username", ""),
            password=value.get("password", ""),
        )
    except ProxyConfigurationError:
        return None


def updated_download_proxy(
    *,
    current: DownloadProxy | None,
    url: object,
    username: object,
    password: object,
    password_action: object,
) -> DownloadProxy:
    action = str(password_action or "clear").strip().lower()
    if action not in {"keep", "replace", "clear"}:
        raise ProxyConfigurationError("代理密码操作无效，请刷新页面后重试。")
    candidate = build_download_proxy(url=url, username=username, password="")
    if action == "keep":
        if (
            current is None
            or candidate.endpoint_url != current.endpoint_url
            or candidate.username != current.username
        ):
            raise ProxyConfigurationError("代理地址或账号已变化，请重新输入密码。")
        selected_password = current.password
    elif action == "replace":
        if not isinstance(password, str) or not password:
            raise ProxyConfigurationError("请输入新的代理密码。")
        selected_password = password
    else:
        selected_password = ""
    return build_download_proxy(
        url=candidate.endpoint_url,
        username=candidate.username,
        password=selected_password,
    )


def open_network_request(
    request: Request,
    *,
    timeout: float,
    proxy: DownloadProxy | None,
) -> NetworkResponse:
    if proxy is None:
        opener = build_opener(ProxyHandler({}))
        return cast(NetworkResponse, opener.open(request, timeout=timeout))
    if proxy.scheme in {"http", "https"}:
        proxy_headers = (
            urllib3.make_headers(
                proxy_basic_auth=f"{proxy.username}:{proxy.password}",
            )
            if proxy.username
            else None
        )
        manager: urllib3.ProxyManager | None = None
        try:
            manager = urllib3.ProxyManager(
                proxy.endpoint_url,
                proxy_headers=proxy_headers,
                cert_reqs="CERT_REQUIRED",
            )
            response = manager.request(
                request.get_method(),
                request.full_url,
                body=request.data,
                headers=dict(request.header_items()),
                timeout=urllib3.Timeout(connect=timeout, read=timeout),
                redirect=True,
                preload_content=False,
                decode_content=False,
            )
        except Exception as exc:
            if manager is not None:
                manager.clear()
            raise URLError("proxy request failed") from exc
        if response.status >= 400:
            status = response.status
            headers = response.headers
            response.close()
            manager.clear()
            raise HTTPError(request.full_url, status, f"HTTP {status}", headers, None)
        return _Urllib3NetworkResponse(manager, response)
    opener = build_opener(
        ProxyHandler({}),
        SocksiPyHandler(
            socks.SOCKS5,
            proxy.host,
            proxy.port,
            rdns=proxy.scheme == "socks5h",
            username=proxy.username or None,
            password=proxy.password or None,
        ),
    )
    return cast(NetworkResponse, opener.open(request, timeout=timeout))


def proxy_process_environment(
    proxy: DownloadProxy | None,
    *,
    base: dict[str, str] | None = None,
    socks_module_directory: Path | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in PROXY_ENVIRONMENT_KEYS:
        env.pop(key, None)
    env["PIP_CONFIG_FILE"] = os.devnull
    if proxy is None:
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"
        return env
    url = proxy.transport_url
    env.update(
        {
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
            "ALL_PROXY": url,
            "http_proxy": url,
            "https_proxy": url,
            "all_proxy": url,
            "PIP_PROXY": url,
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }
    )
    if proxy.scheme in {"socks5", "socks5h"}:
        if socks_module_directory is None:
            raise ProxyConfigurationError("SOCKS5 subprocess support was not prepared.")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(socks_module_directory.resolve()), existing) if item
        )
    return env


def stage_pysocks_module(directory: Path) -> Path:
    source_name = getattr(socks, "__file__", None)
    if not source_name:
        raise OSError("The installed PySocks module could not be located")
    source = Path(source_name).resolve()
    if not source.is_file():
        raise OSError("The installed PySocks module file is missing")
    target_directory = directory.resolve()
    target_directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target_directory / "socks.py")
    return target_directory


_CREDENTIAL_URL_PATTERN = re.compile(r"(?i)\b(https?|socks5h?)://[^\s/?#]*@")


def redact_proxy_credentials(value: str, proxy: DownloadProxy | None = None) -> str:
    text = value
    if proxy is not None and proxy.username:
        for private_url in {proxy.transport_url, unquote(proxy.transport_url)}:
            text = text.replace(private_url, proxy.display_url)
        basic_token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode(
            "ascii"
        )
        text = text.replace(basic_token, "***")
    text = _CREDENTIAL_URL_PATTERN.sub(r"\1://***@", text)
    if proxy is not None and proxy.password:
        text = text.replace(proxy.password, "••••")
    return text


def test_download_proxy(proxy: DownloadProxy) -> tuple[int, str]:
    request = Request(
        PROXY_TEST_URL,
        headers={
            "Accept": "text/plain",
            "Range": "bytes=0-0",
            "User-Agent": "local-video-cutter-proxy-test",
        },
    )
    started = time.monotonic()
    with open_network_request(request, timeout=15, proxy=proxy) as response:
        response.read(1)
    latency_ms = max(1, round((time.monotonic() - started) * 1_000))
    return latency_ms, "代理连接成功，可以用于下载 AI 模型。"
