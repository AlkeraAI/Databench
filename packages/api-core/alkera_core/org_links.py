"""The ``?org=`` hint a link into the web app carries.

A person who belongs to several orgs opens a link from an email, a Slack
thread, a refusal or a status pane in whichever org their browser happens to
be in. A link that names the org the thing it opens lives in lets the web app
offer the switch instead of showing that org's page under the wrong session.

With multi-org off a person holds one org, so the hint could only name the org
they are already in, and links keep the shape they had before multi-org.
"""

from __future__ import annotations

from urllib.parse import urlencode

from alkera_core.auth.tenancy import multi_org_enabled


def org_link_query(org_id: object | None) -> str:
    """``?org=<org id>`` naming ``org_id``; nothing with multi-org off or with
    no org to name."""
    if org_id is None or not multi_org_enabled():
        return ""
    return f"?{urlencode({'org': str(org_id)})}"


__all__ = ["org_link_query"]
