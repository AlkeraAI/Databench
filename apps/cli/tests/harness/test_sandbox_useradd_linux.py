"""The chat user is really created in the reserved uid range.

Runs the real ``useradd`` the box runs, so it needs Linux and root; it is
skipped everywhere else. The argv-level pin lives in ``test_sandbox.py``."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid

import pytest
from alkera_cli.harness import sandbox as sb

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or os.geteuid() != 0
    or shutil.which("useradd") is None
    or shutil.which("userdel") is None,
    reason="creates a real system user: Linux, root and shadow-utils only",
)


def test_a_real_chat_user_lands_in_the_reserved_range_with_a_matching_group() -> None:
    import grp
    import pwd

    chat_id = f"t{uuid.uuid4().hex[:12]}"
    name = sb.chat_user(chat_id)
    try:
        uid = sb.ensure_chat_uid(chat_id)
        entry = pwd.getpwnam(name)
        assert sb.UID_MIN <= uid <= sb.UID_MAX
        assert entry.pw_uid == uid
        assert sb.UID_MIN <= entry.pw_gid <= sb.UID_MAX
        assert grp.getgrgid(entry.pw_gid).gr_name == name
        assert entry.pw_shell == "/usr/sbin/nologin"
        assert not os.path.exists(entry.pw_dir) or entry.pw_dir == "/"
        # A second call finds the same user rather than making another.
        assert sb.ensure_chat_uid(chat_id) == uid
    finally:
        subprocess.run(["userdel", name], check=False, capture_output=True)
    with pytest.raises(KeyError):
        pwd.getpwnam(name)
