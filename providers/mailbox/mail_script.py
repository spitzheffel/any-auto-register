"""Mail 脚本接口 — register into unified registry."""
from core.mail_script_mailbox import MailScriptMailbox  # noqa: F401
from providers.registry import register_provider

register_provider("mailbox", "mail_script_api")(MailScriptMailbox)
