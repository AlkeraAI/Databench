# Copyright 2026 Marimo. All rights reserved.
# Modified by Alkera: import paths rewritten; see vendor/marimo/README.alkera.md
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from alkera_notebook._marimo._messaging.notification import SecretKeysResultNotification
from alkera_notebook._marimo._messaging.notification_utils import broadcast_notification
from alkera_notebook._marimo._runtime.commands import (
    ListSecretKeysCommand,
    RefreshSecretsCommand,
)
from alkera_notebook._marimo._secrets.secrets import get_secret_keys

if TYPE_CHECKING:
    from alkera_notebook._marimo._runtime.request_router import RequestRouter
    from alkera_notebook._marimo._runtime.runtime import Kernel


class SecretsCallbacks:
    def __init__(self, kernel: Kernel):
        self._kernel = kernel
        # Snapshot at startup so dotenv-loaded keys can later be distinguished from inherited ones.
        self._original_environ = os.environ.copy()

    def register(self, router: RequestRouter) -> None:
        router.register(ListSecretKeysCommand, self.list_secrets)
        router.register(RefreshSecretsCommand, self.refresh_secrets)

    async def list_secrets(self, request: ListSecretKeysCommand) -> None:
        secrets = get_secret_keys(
            self._kernel.user_config, self._original_environ
        )
        broadcast_notification(
            SecretKeysResultNotification(
                request_id=request.request_id, secrets=secrets
            ),
        )

    async def refresh_secrets(self, request: RefreshSecretsCommand) -> None:
        del request
        self._kernel.load_dotenv()
