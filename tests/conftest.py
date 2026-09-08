"""Shared test fixtures.

Uses a temporary file-based SQLite database with check_same_thread=False
so that the app's background threads (scheduler, task_runtime) can share it.

外呼一律阻断（见 _block_outbound_network / _block_proxied_requests）：接码取号与云打码
都是按次计费的，一处打桩写漏就会真花钱，所以在 socket 层兜底而不是依赖每个测试自觉。
socket 层放行本地地址是为了 FastAPI TestClient；但 requests 走本地代理
（如 provider 设置里的 http://127.0.0.1:7890）时 socket 只看得到代理端口，
所以再在 requests 适配器层把带代理的请求拦掉。

这两层拦截只对本进程生效，不覆盖子进程（Playwright/Camoufox 浏览器）的出网。
"""
from __future__ import annotations

import os
import socket
import tempfile

# Create a temp DB file BEFORE any application code imports core.db
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
_TEST_DB_PATH = _tmp.name
os.environ["ACCOUNT_MANAGER_DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"

import pytest
from sqlmodel import SQLModel, create_engine

# Patch the engine before the app is created
from core import db as _db_module

_db_module.engine = create_engine(
    f"sqlite:///{_TEST_DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


class OutboundNetworkBlocked(RuntimeError):
    """测试试图访问外部网络——通常意味着某个 provider 没被打桩。"""


def _is_local_host(host) -> bool:
    # getaddrinfo(None, port) / bind 时 host 可能是 None、b"" 或字节形式的主机名，
    # 直接 str() 会得到 "None"/"b'localhost'"，永远匹配不上集合
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("utf-8", "ignore")
    return str(host) in _LOCAL_HOSTS


def _is_local_address(address) -> bool:
    if isinstance(address, (str, bytes)):  # AF_UNIX
        return True
    if isinstance(address, tuple) and address:
        return _is_local_host(address[0])
    return False


@pytest.fixture(scope="session", autouse=True)
def _block_outbound_network():
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address, *args, **kwargs):
        if not _is_local_address(address):
            raise OutboundNetworkBlocked(
                f"测试禁止外呼: {address!r}。付费接口（接码取号 / 云打码）必须打桩，"
                "请 patch 对应的 provider 工厂或 requests 层。"
            )
        return real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        if not _is_local_address(address):
            raise OutboundNetworkBlocked(f"测试禁止外呼: {address!r}")
        return real_connect_ex(self, address, *args, **kwargs)

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        # 提前在解析阶段失败，避免被上游超时/重试拖成几十秒
        if not _is_local_host(host):
            raise OutboundNetworkBlocked(
                f"测试禁止解析外部域名: {host!r}。相关 provider 未打桩。"
            )
        return real_getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.getaddrinfo = guarded_getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
        socket.getaddrinfo = real_getaddrinfo


@pytest.fixture(scope="session", autouse=True)
def _block_proxied_requests():
    """requests 一旦选中了代理就拦掉，不管代理地址是不是本地。

    走代理时 socket 层只能看到 127.0.0.1:7890 这样的代理端口，会被当成本地直连放行，
    实际流量却出网了。HTTPAdapter.send 拿到的 proxies 已经是显式参数与环境变量
    （HTTP_PROXY / HTTPS_PROXY，含 NO_PROXY 过滤）合并后的最终结果，在这里判断
    和 requests 自己选代理的口径完全一致。无代理的本地直连不受影响。
    """
    from requests.adapters import HTTPAdapter
    from requests.utils import select_proxy

    real_send = HTTPAdapter.send

    def guarded_send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        proxy = select_proxy(request.url, proxies or {})
        if proxy:
            raise OutboundNetworkBlocked(
                f"测试禁止经代理外呼: {request.url} via {proxy}。付费接口（接码取号 / 云打码）"
                "必须打桩，请 patch 对应的 provider 工厂或 requests 层。"
            )
        return real_send(self, request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)

    HTTPAdapter.send = guarded_send
    try:
        yield
    finally:
        HTTPAdapter.send = real_send


@pytest.fixture(scope="session", autouse=True)
def _disable_turnstile_solver():
    """测试不启动本地 Turnstile solver。

    应用 lifespan 会 spawn 一个 Camoufox 子进程并在关闭时按端口清理，
    单次 client fixture 因此要花 30 秒以上——测试既不需要真解题，
    也不该反复拉起浏览器进程。
    """
    from services import solver_manager

    original_start = solver_manager.start_async
    original_stop = solver_manager.stop
    solver_manager.start_async = lambda *args, **kwargs: None
    solver_manager.stop = lambda *args, **kwargs: None
    try:
        yield
    finally:
        solver_manager.start_async = original_start
        solver_manager.stop = original_stop


@pytest.fixture(autouse=True)
def _reset_db():
    """Drop and recreate all tables between tests for full isolation."""
    SQLModel.metadata.drop_all(_db_module.engine)
    SQLModel.metadata.create_all(_db_module.engine)
    yield


@pytest.fixture()
def client():
    """FastAPI TestClient with a clean database."""
    from main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


from fastapi.testclient import TestClient


def pytest_sessionfinish(session, exitstatus):
    """Clean up temp DB file."""
    try:
        os.unlink(_TEST_DB_PATH)
    except OSError:
        pass
