class DeliveryError(RuntimeError):
    """A transport result with an explicit failed, stopped or unknown status."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


class ReplyRequired(DeliveryError):
    """The platform rejected unsolicited delivery for this recipient."""

    def __init__(self):
        super().__init__("failed", "QQ 官方暂不允许主动发送，等待对方发来新消息")


class ConversationGone(ValueError):
    """The originally pinned conversation no longer exists for this recipient."""
