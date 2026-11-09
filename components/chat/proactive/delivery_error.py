class DeliveryError(RuntimeError):
    """A transport result with an explicit failed, stopped or unknown status."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


class ConversationGone(ValueError):
    """The originally pinned conversation no longer exists for this recipient."""
