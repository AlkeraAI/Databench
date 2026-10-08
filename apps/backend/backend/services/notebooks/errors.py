"""What the notebook service refuses with: each error a route or the socket
turns into its answer."""

from __future__ import annotations


class NotebookUnavailableError(Exception):
    """This process runs no CRDT lane (or one without notebooks)."""


class OpRefusedError(Exception):
    """A batch the document refused, naming the op and why."""

    def __init__(self, code: str, message: str, op_index: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.op_index = op_index


class KernelAnswerError(Exception):
    """The engine answered a request with a refusal (``code``, ``message``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class KernelSilentError(Exception):
    """The engine did not answer a request in time."""


class NoMachineError(Exception):
    """No machine holds the notebook's folder, so nothing can run it."""


class CommRefusedError(Exception):
    """A widget message from the notebook channel that was not carried:
    ``code`` is what the socket answers; ``lost`` says the sender may no
    longer read the notebook, so the channel is dropped too."""

    def __init__(self, code: str, message: str, *, lost: bool = False) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.lost = lost


class AgentChatRefusedError(Exception):
    """A box named a chat whose agent it says made a batch, and the chat is
    not one it may write for: not a chat of the notebook's org bound to that
    box, not in the notebook's workspace, or its person no longer stands."""


__all__ = [
    "AgentChatRefusedError",
    "CommRefusedError",
    "KernelAnswerError",
    "KernelSilentError",
    "NoMachineError",
    "NotebookUnavailableError",
    "OpRefusedError",
]
