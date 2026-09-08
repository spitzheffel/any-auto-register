"""GenericHttpMailbox — register into unified registry.

数据驱动的通用邮箱：端点与认证全部来自 provider definition 的 metadata 管道，
新增一个"只是 HTTP 接口不同"的邮箱服务无需写代码。
"""
from core.generic_http_mailbox import GenericHttpMailbox  # noqa: F401
from providers.registry import register_provider

register_provider("mailbox", "generic_http_mailbox", aliases=("generic_http",))(GenericHttpMailbox)
