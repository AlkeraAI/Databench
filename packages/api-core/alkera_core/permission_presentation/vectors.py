"""Conformance vectors: asks every surface must present identically.

Each vector is a named :class:`PermissionAsk` (as camelCase JSON). The export
pairs it with :func:`present`'s output and commits the pairs into
``@alkera/chat-model``, where the TypeScript twin must reproduce every one
exactly; the Slack tests render every one and check each primary field lands.
Every registered presenter must be exercised by at least one vector (pinned in
``packages/api-core/tests/permission_presentation``), so a new entry arrives
with its proof.
"""

from __future__ import annotations

from typing import Any

_ALLOW = [
    {"optionId": "allow_once", "name": "Allow once"},
    {"optionId": "allow_always", "name": "Always allow"},
    {"optionId": "reject_once", "name": "Reject once"},
]
_ONCE = [
    {"optionId": "allow_once", "name": "Allow once"},
    {"optionId": "reject_once", "name": "Reject once"},
]


#: Where a notebook lives on the machine that runs it: a path no reader is shown.
_BOX_NOTEBOOK = "/opt/alkera-work/.alkera/workspaces/w1/files/analysis.alknb.py"


def _subject(**fields: Any) -> dict[str, Any]:
    return {"targets": [], **fields}


VECTORS: dict[str, dict[str, Any]] = {
    "shell_named": {
        "permissionKind": "bash",
        "canonicalKind": "shell",
        "patterns": ["git status"],
        "subject": _subject(capability="shell", effect="read", operation="git_status"),
        "options": _ALLOW,
    },
    "shell_unnamed": {
        "permissionKind": "bash",
        "canonicalKind": "shell",
        "patterns": [],
        "options": _ONCE,
    },
    "shell_waiting": {
        "permissionKind": "bash",
        "canonicalKind": "shell",
        "patterns": [],
        "subjectPending": True,
        "options": _ONCE,
    },
    "shell_recovered_from_call": {
        "permissionKind": "bash",
        "canonicalKind": "shell",
        "patterns": [],
        "options": _ONCE,
        "call": {"name": "bash", "input": {"command": "ls -la /opt/alkera-work"}},
    },
    "shell_secrets": {
        "permissionKind": "bash",
        "canonicalKind": "shell",
        "patterns": [
            "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwx0123 curl -H 'Authorization: Bearer "
            "abc.def.ghi' https://deploy:hunter2@example.com/hook --password s3cret"
        ],
        "options": _ONCE,
    },
    "shell_exact_command": {
        "permissionKind": "bash",
        "canonicalKind": "shell",
        "patterns": ["rm -rf build/"],
        "subject": _subject(capability="shell", effect="destroy", operation="rm"),
        "options": [
            {"optionId": "allow_once", "name": "Allow once"},
            {"optionId": "allow_always", "name": "Always allow this exact command"},
            {"optionId": "reject_once", "name": "Deny"},
        ],
    },
    "shell_long": {
        "permissionKind": "bash",
        "canonicalKind": "shell",
        "patterns": ["\n".join(f"echo line {i}" for i in range(1, 201))],
        "options": _ONCE,
    },
    "edit_diff": {
        "permissionKind": "edit",
        "canonicalKind": "edit",
        "patterns": ["src/app.ts"],
        "preview": {
            "kind": "diff",
            "title": "app.ts",
            "content": "@@ -1,1 +1,1 @@\n-const key = 'old'\n+const key = 'new'\n",
        },
        "subject": _subject(capability="fs", effect="write", operation="edit"),
        "options": _ALLOW,
    },
    "edit_unnamed": {
        "permissionKind": "edit",
        "canonicalKind": "edit",
        "patterns": [],
        "options": _ONCE,
    },
    "network_named": {
        "permissionKind": "webfetch",
        "canonicalKind": "network",
        "patterns": ["https://example.com/feed"],
        "options": _ALLOW,
    },
    "task_named": {
        "permissionKind": "task",
        "canonicalKind": "task",
        "patterns": ["spawn a reviewer subagent"],
        "options": _ONCE,
    },
    "external_tool": {
        "permissionKind": "mcp__alkera__blob.query",
        "canonicalKind": "external",
        "patterns": ["select 1"],
        "options": _ONCE,
    },
    "generic_named_tool": {
        "permissionKind": "alkera_blob_query",
        "canonicalKind": "other",
        "patterns": ["select * from results"],
        "options": _ONCE,
    },
    "generic_unknown_tool": {
        "permissionKind": "acme_deploy",
        "canonicalKind": "other",
        "patterns": [],
        "options": _ONCE,
        "call": {
            "name": "mcp__acme__deploy",
            "input": {
                "service": "billing",
                "replicas": 3,
                "ratio": 0.5,
                "dryRun": False,
                "api_key": "sk-live-abcdefghijklmnop",
                "env": {"DATABASE_PASSWORD": "pw", "REGION": "us-east-1"},
                "notes": "rotate STRIPE_SECRET=abc123 after",
            },
        },
    },
    "generic_unknown_bare": {
        "permissionKind": "some_future_guard",
        "canonicalKind": "other",
        "patterns": [],
        "options": _ONCE,
    },
    "sql_destroy_with_cost": {
        "permissionKind": "mcp__alkera__sql.query",
        "canonicalKind": "other",
        "patterns": ["delete from analytics.orders where id = 7"],
        "subject": _subject(
            capability="sql",
            effect="destroy",
            operation="delete",
            targets=[{"kind": "table", "name": "analytics.orders", "connection": "snow-prod"}],
            cost={"usd": 0.425, "bytesScanned": 1288490188},
        ),
        "options": _ALLOW,
    },
    "sql_egress_rows": {
        "permissionKind": "sql",
        "canonicalKind": "other",
        "patterns": ["copy into @stage from orders"],
        "subject": _subject(
            capability="sql",
            effect="egress",
            operation="copy_into",
            cost={"usd": 3, "rowsScanned": 1234567, "currency": "credits"},
        ),
        "options": _ALLOW,
    },
    "sql_exec": {
        "permissionKind": "mcp__alkera__sql.query",
        "canonicalKind": "other",
        "patterns": ["COPY t TO '/tmp/out.csv'"],
        "subject": _subject(capability="sql", effect="exec", operation="copy_file"),
        "options": _ONCE,
    },
    "sql_by_kind_alone": {
        "permissionKind": "sql",
        "canonicalKind": "other",
        "patterns": [],
        "options": _ONCE,
        "call": {"name": "sql.query", "input": {"sql": "insert into t values (1)"}},
    },
    "knowledge_share": {
        "permissionKind": "knowledge",
        "canonicalKind": "other",
        "patterns": ["Share with the Team Knowledge Base:\nRevenue\nRefunds are netted."],
        "subject": _subject(
            capability="knowledge",
            effect="egress",
            operation="knowledge_share",
            targets=[{"kind": "knowledge", "name": "Team Knowledge Base"}],
        ),
        "preview": {
            "kind": "text",
            "title": "Revenue",
            "content": "Refunds are **netted** out of revenue.\n\nOn the day they settle.",
        },
        "options": _ALLOW,
    },
    "knowledge_unshare": {
        "permissionKind": "knowledge",
        "canonicalKind": "other",
        "patterns": ["withdraw 'fact:revenue' from the Team Knowledge Base"],
        "subject": _subject(capability="knowledge", effect="egress", operation="knowledge_unshare"),
        "options": _ALLOW,
    },
    "knowledge_note": {
        "permissionKind": "knowledge",
        "canonicalKind": "other",
        "patterns": ["Save a note"],
        "subject": _subject(capability="knowledge", effect="memory", operation="knowledge_note"),
        "preview": {"kind": "text", "content": "Churn is measured monthly.", "truncated": True},
        "options": _ALLOW,
    },
    "mongodb_destroy": {
        "permissionKind": "mongodb",
        "canonicalKind": "other",
        "patterns": ['[{"$merge": "orders"}]'],
        "subject": _subject(capability="mongodb", effect="destroy", operation="merge"),
        "options": _ALLOW,
    },
    "elasticsearch_write": {
        "permissionKind": "elasticsearch",
        "canonicalKind": "other",
        "patterns": ["PUT /logs/_settings"],
        "subject": _subject(capability="elasticsearch", effect="write", operation="put_settings"),
        "options": _ALLOW,
    },
    "integration_sdk_on_connection": {
        "permissionKind": "integration_sdk",
        "canonicalKind": "other",
        "patterns": ["for job in client.jobs.list():\n    print(job.name)"],
        "subject": _subject(
            capability="integration_sdk",
            effect="destroy",
            operation="call_integration_sdk",
            targets=[{"kind": "connection", "name": "dbx-prod", "connection": "dbx-prod"}],
        ),
        "options": _ALLOW,
    },
    "graph_file": {
        "permissionKind": "graph",
        "canonicalKind": "other",
        "patterns": ["create graph orders"],
        "subject": _subject(capability="file", effect="write", operation="graph_create"),
        "options": _ALLOW,
    },
    "connection_action": {
        "permissionKind": "snowflake",
        "canonicalKind": "other",
        "patterns": ["suspend warehouse WH_1"],
        "subject": _subject(
            capability="snowflake",
            effect="write",
            operation="suspend_warehouse",
            targets=[{"kind": "warehouse", "name": "WH_1", "connection": "snow-prod"}],
        ),
        "options": _ALLOW,
    },
    "connection_action_unbound": {
        "permissionKind": "databricks",
        "canonicalKind": "other",
        "patterns": ["run job 42"],
        "subject": _subject(capability="databricks", effect="write", operation="run_job"),
        "options": _ALLOW,
    },
    "mechanism_external_directory": {
        "permissionKind": "external_directory",
        "canonicalKind": "external",
        "patterns": ["/opt/alkera-work/notes.md"],
        "options": _ONCE,
    },
    "mechanism_repo_clone": {
        "permissionKind": "repo_clone",
        "canonicalKind": "external",
        "patterns": ["https://github.com/acme/app"],
        "options": _ONCE,
    },
    "mechanism_repo_overview": {
        "permissionKind": "repo_overview",
        "canonicalKind": "other",
        "patterns": ["acme/app"],
        "options": _ONCE,
    },
    "mechanism_doom_loop": {
        "permissionKind": "doom_loop",
        "canonicalKind": "shell",
        "patterns": ["npm test"],
        "options": _ONCE,
    },
    "mechanism_workflow_step": {
        "permissionKind": "workflow_tool_approval",
        "canonicalKind": "shell",
        "patterns": ["deploy staging"],
        "options": _ONCE,
    },
    "notebook_run": {
        "permissionKind": "notebook",
        "canonicalKind": "other",
        "patterns": ["Run 2 cells in analysis.alknb.py"],
        "subject": _subject(
            capability="notebook",
            effect="write",
            confidence="unknown",
            operation="notebook_run",
            scope="command",
            targets=[{"kind": "notebook", "name": _BOX_NOTEBOOK}],
        ),
        "preview": {
            "kind": "notebook",
            "title": "Run 2 cells in analysis.alknb.py",
            "truncated": False,
            "notebook": {
                "lead": "Run 2 cells in",
                "fileName": "analysis.alknb.py",
                "filePath": "analysis.alknb.py",
                "cells": [
                    {"name": "setup", "code": "import alkera", "role": "dependency"},
                    {
                        "name": "Cell 2",
                        "code": (
                            "import polars as pl\n\nAPI_TOKEN = 'abc123'\ndf = pl.read_csv('x')"
                        ),
                        "role": "target",
                    },
                    {"name": "load", "code": "rows = " + "1 + " * 40 + "1", "role": "target"},
                ],
            },
        },
        "options": _ONCE,
    },
    "notebook_run_always": {
        "permissionKind": "notebook",
        "canonicalKind": "other",
        "patterns": ["Run 1 cell in reports/q3.alknb.py"],
        "subject": _subject(
            capability="notebook",
            effect="write",
            confidence="exact",
            operation="notebook_run",
            scope="command",
            targets=[
                {"kind": "notebook", "name": "/opt/alkera-work/reports/q3.alknb.py"},
                {"kind": "table", "name": "orders", "connection": "warehouse"},
            ],
        ),
        "preview": {
            "kind": "notebook",
            "notebook": {
                "lead": "Run 1 cell in",
                "fileName": "q3.alknb.py",
                "filePath": "reports/q3.alknb.py",
                "cells": [
                    {"name": "Cell 4", "code": "CREATE TABLE orders AS SELECT 1", "role": "target"},
                    {"name": "chart", "code": "alkera.chart(orders)", "role": "dependent"},
                    {"name": "Cell 6", "code": "print(len(orders))", "role": "dependent"},
                ],
            },
        },
        "options": _ALLOW,
    },
    "notebook_install": {
        "permissionKind": "notebook",
        "canonicalKind": "other",
        "patterns": ["Install polars into the environment of analysis.alknb.py"],
        "subject": _subject(capability="notebook", effect="egress", operation="notebook_install"),
        "preview": {
            "kind": "notebook",
            "notebook": {
                "lead": "Install polars into the environment of",
                "fileName": "analysis.alknb.py",
                "filePath": "analysis.alknb.py",
                "packages": ["polars"],
            },
        },
        "options": _ONCE,
    },
    "notebook_kernel_others": {
        "permissionKind": "notebook",
        "canonicalKind": "other",
        "patterns": ["Interrupt the kernel of analysis.alknb.py (ends work started by Bob)"],
        "subject": _subject(capability="notebook", effect="destroy", operation="notebook_kernel"),
        "preview": {
            "kind": "notebook",
            "notebook": {
                "lead": "Interrupt the kernel of",
                "fileName": "analysis.alknb.py",
                "filePath": "analysis.alknb.py",
                "tail": " (ends work started by Bob)",
            },
        },
        "options": _ONCE,
    },
    "notebook_without_detail": {
        "permissionKind": "notebook",
        "canonicalKind": "other",
        "patterns": ["Run 1 cell in analysis.alknb.py"],
        "subject": _subject(capability="notebook", effect="write", operation="notebook_run"),
        "options": _ONCE,
    },
}


__all__ = ["VECTORS"]
