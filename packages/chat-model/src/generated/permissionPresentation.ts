/* eslint-disable */
/**
 * Auto-generated from alkera_core.permission_presentation (the registry,
 * the redaction rules and the permission modes). Do not edit by hand:
 * change the Python registry and run `make gen-tool-manifest`.
 */

export const NATIVE_TOOL_NAMES = [
  "read",
  "write",
  "edit",
  "glob",
  "grep",
  "bash",
  "apply_patch",
  "lsp",
  "repo_clone",
  "repo_overview",
  "webfetch",
  "websearch",
  "todowrite",
  "skill"
] as const;

export const PERMISSION_PRESENTATION_REGISTRY = {
  "version": "1.1.0",
  "execTitle": "Allow this to run a program or use files on the database server?",
  "exactAlwaysLabel": "Always allow this exact command",
  "decisionOutcomes": {
    "allow_once": "Allowed once",
    "allow_always": "Always allowed",
    "reject_once": "Rejected",
    "reject_always": "Always rejected",
    "cancelled": "Cancelled"
  },
  "effectReasons": {
    "read": "Only reads files or data",
    "write": "May change files or data",
    "destroy": "May delete files or data",
    "egress": "May send or fetch data over the network",
    "exec": "May run programs on the database server",
    "memory": "May change what the agent remembers"
  },
  "operationReasons": {
    "notebook_kernel": "Stops code running in the notebook",
    "notebook_install": "Downloads packages from the internet"
  },
  "unsureReason": "Can't tell if it changes anything",
  "notebookPreviewLines": 2,
  "notebookPreviewWidth": 80,
  "subjectInputKeys": [
    "command",
    "path",
    "filePath",
    "file_path",
    "file",
    "url"
  ],
  "redacted": "[redacted]",
  "redactionRules": [
    {
      "name": "assignment",
      "pattern": "\\b([A-Za-z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIALS?)[A-Za-z0-9_]*[ \\t]*[=:][ \\t]*)(\"[^\"\\n]*\"|'[^'\\n]*'|[^\\s\"',;&|]+)()",
      "ignoreCase": true
    },
    {
      "name": "authorization",
      "pattern": "(authorization:[ \\t]*(?:bearer|basic|token)[ \\t]+)([A-Za-z0-9._~+/=-]+)()",
      "ignoreCase": true
    },
    {
      "name": "flag",
      "pattern": "(--?(?:password|passwd|token|secret|api-key|apikey|access-key|client-secret)(?:=|[ \\t]+))(\"[^\"\\n]*\"|'[^'\\n]*'|[^\\s\"']+)()",
      "ignoreCase": true
    },
    {
      "name": "url_userinfo",
      "pattern": "([a-z][a-z0-9+.-]*://[^\\s/:@]+:)([^\\s/@]+)(@)",
      "ignoreCase": true
    },
    {
      "name": "token_shape",
      "pattern": "()((?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|alk_[A-Za-z0-9_]{16,}|AIza[0-9A-Za-z_-]{35}|eyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,})()",
      "ignoreCase": false
    }
  ],
  "secretKeyPattern": "^(?:.*[_-])?(?:secret|token|password|passwd|api[_-]?key|apikey|private[_-]?key|access[_-]?key|credentials?|authorization|cookie)$",
  "nativeToolAliases": {
    "ripgrep": "grep",
    "shell": "bash",
    "terminal": "bash",
    "patch": "apply_patch",
    "todo": "todowrite"
  },
  "presenters": [
    {
      "key": "shell",
      "layer": "canonical",
      "matches": [
        "shell"
      ],
      "label": "Run a shell command",
      "titles": [
        {
          "text": "Run this command?",
          "effect": null,
          "operation": null,
          "named": true
        },
        {
          "text": "Run a command?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "command",
      "language": "bash",
      "waiting": "Waiting for the command…",
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "edit",
      "layer": "canonical",
      "matches": [
        "edit"
      ],
      "label": "Edit files",
      "titles": [
        {
          "text": "Edit these files?",
          "effect": null,
          "operation": null,
          "named": true
        },
        {
          "text": "Edit a file?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "path",
      "language": null,
      "waiting": "Waiting for the file…",
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "network",
      "layer": "canonical",
      "matches": [
        "network"
      ],
      "label": "Fetch a web address",
      "titles": [
        {
          "text": "Fetch this address?",
          "effect": null,
          "operation": null,
          "named": true
        },
        {
          "text": "Fetch an address?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "url",
      "language": null,
      "waiting": "Waiting for the address…",
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "task",
      "layer": "canonical",
      "matches": [
        "task"
      ],
      "label": "Delegate to a subagent",
      "titles": [
        {
          "text": "Delegate this work?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "code",
      "language": null,
      "waiting": "Waiting for the work…",
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "external",
      "layer": "canonical",
      "matches": [
        "external"
      ],
      "label": "Use an external tool",
      "titles": [
        {
          "text": "Allow {tool}?",
          "effect": null,
          "operation": null,
          "named": null
        },
        {
          "text": "Allow this action?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "code",
      "language": null,
      "waiting": "Waiting for the request…",
      "namesTool": true,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "generic",
      "layer": "canonical",
      "matches": [
        "other"
      ],
      "label": "Use a tool",
      "titles": [
        {
          "text": "Allow {tool}?",
          "effect": null,
          "operation": null,
          "named": null
        },
        {
          "text": "Allow this action?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "code",
      "language": null,
      "waiting": "Waiting for the request…",
      "namesTool": true,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "shell_lane",
      "layer": "capability",
      "matches": [
        "shell"
      ],
      "label": null,
      "titles": [],
      "titlesWhenCanonical": [],
      "format": "code",
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "command",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "fs_lane",
      "layer": "capability",
      "matches": [
        "fs"
      ],
      "label": null,
      "titles": [],
      "titlesWhenCanonical": [],
      "format": "code",
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "file action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "network_lane",
      "layer": "capability",
      "matches": [
        "network"
      ],
      "label": null,
      "titles": [],
      "titlesWhenCanonical": [],
      "format": "code",
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "request",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "sql",
      "layer": "capability",
      "matches": [
        "sql"
      ],
      "label": "Run a SQL query",
      "titles": [
        {
          "text": "Run this destructive query?",
          "effect": "destroy",
          "operation": null,
          "named": null
        },
        {
          "text": "Run this query? It exports data.",
          "effect": "egress",
          "operation": null,
          "named": null
        },
        {
          "text": "Run this query?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "code",
      "language": "sql",
      "waiting": null,
      "namesTool": false,
      "inputKeys": [
        "sql",
        "query",
        "statement"
      ],
      "scopeNoun": "statement",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "knowledge",
      "layer": "capability",
      "matches": [
        "knowledge"
      ],
      "label": "Write to a knowledge base",
      "titles": [
        {
          "text": "Share with the Team Knowledge Base?",
          "effect": null,
          "operation": "knowledge_share",
          "named": null
        },
        {
          "text": "Withdraw from the Team Knowledge Base?",
          "effect": null,
          "operation": "knowledge_unshare",
          "named": null
        },
        {
          "text": "Save to the Project Knowledge Base?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "sentence",
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": "save to the Project Knowledge Base",
      "scopePhrases": {
        "knowledge_share": "share with the Team Knowledge Base",
        "knowledge_unshare": "withdrawal from the Team Knowledge Base"
      }
    },
    {
      "key": "mongodb",
      "layer": "capability",
      "matches": [
        "mongodb"
      ],
      "label": "Run a MongoDB aggregation",
      "titles": [
        {
          "text": "Run this destructive aggregation?",
          "effect": "destroy",
          "operation": null,
          "named": null
        },
        {
          "text": "Run this aggregation?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "code",
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "aggregation",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "elasticsearch",
      "layer": "capability",
      "matches": [
        "elasticsearch"
      ],
      "label": "Send a search request",
      "titles": [
        {
          "text": "Send this destructive request?",
          "effect": "destroy",
          "operation": null,
          "named": null
        },
        {
          "text": "Send this request?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "code",
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "request",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "integration_sdk",
      "layer": "capability",
      "matches": [
        "integration_sdk"
      ],
      "label": "Run code against a connection",
      "titles": [
        {
          "text": "Run this code against {connection}?",
          "effect": null,
          "operation": null,
          "named": null
        },
        {
          "text": "Run this code?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "code",
      "language": "python",
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "SDK call",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "graph_file",
      "layer": "capability",
      "matches": [
        "file"
      ],
      "label": "Write a graph file",
      "titles": [
        {
          "text": "Write this graph file?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "sentence",
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "notebook",
      "layer": "capability",
      "matches": [
        "notebook"
      ],
      "label": "Use a notebook",
      "titles": [
        {
          "text": "Allow this notebook action?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": "sentence",
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "notebook action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "connection_action",
      "layer": "capability",
      "matches": [
        "*"
      ],
      "label": "Act on a connection",
      "titles": [
        {
          "text": "Allow this action on {connection}?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [
        "other"
      ],
      "format": "sentence",
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "external_directory",
      "layer": "mechanism",
      "matches": [
        "external_directory"
      ],
      "label": "Reach outside the project",
      "titles": [
        {
          "text": "Allow access outside the project?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": null,
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "repo_clone",
      "layer": "mechanism",
      "matches": [
        "repo_clone"
      ],
      "label": "Clone a repository",
      "titles": [
        {
          "text": "Clone this repository?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": null,
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "repo_overview",
      "layer": "mechanism",
      "matches": [
        "repo_overview"
      ],
      "label": "Read a cached repository",
      "titles": [
        {
          "text": "Read this cached repository?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": null,
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "doom_loop",
      "layer": "mechanism",
      "matches": [
        "doom_loop"
      ],
      "label": "Repeat a call",
      "titles": [
        {
          "text": "Repeat this call again?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": null,
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    },
    {
      "key": "workflow_tool_approval",
      "layer": "mechanism",
      "matches": [
        "workflow_tool_approval"
      ],
      "label": "Run a workflow step",
      "titles": [
        {
          "text": "Allow this workflow step?",
          "effect": null,
          "operation": null,
          "named": null
        }
      ],
      "titlesWhenCanonical": [],
      "format": null,
      "language": null,
      "waiting": null,
      "namesTool": false,
      "inputKeys": [],
      "scopeNoun": "action",
      "scopeFallback": null,
      "scopePhrases": {}
    }
  ],
  "modes": [
    {
      "value": "default",
      "label": "Default",
      "description": "Runs reads freely; asks before changes outside the chat's files.",
      "writes": true,
      "rank": 2,
      "short": null,
      "aliases": [
        "ask"
      ]
    },
    {
      "value": "plan",
      "label": "Plan",
      "description": "Explores, writes only in the chat's files, and proposes a plan.",
      "writes": false,
      "rank": 1,
      "short": null,
      "aliases": []
    },
    {
      "value": "auto",
      "label": "Auto",
      "description": "Works on its own and pauses only for risky or destructive steps.",
      "writes": true,
      "rank": 3,
      "short": null,
      "aliases": []
    },
    {
      "value": "read_only",
      "label": "Read-only",
      "description": "Reads freely, writes only in this chat, and runs no shell.",
      "writes": false,
      "rank": 0,
      "short": null,
      "aliases": [
        "read-only",
        "readonly"
      ]
    },
    {
      "value": "bypass",
      "label": "Bypass permissions",
      "description": "Runs everything without asking.",
      "writes": true,
      "rank": 4,
      "short": "Bypass",
      "aliases": []
    }
  ],
  "outcomes": {
    "surfacePhrases": {
      "web": "on web",
      "slack": "in Slack"
    },
    "resolutionVerbs": {
      "allow_once": "Allowed",
      "allow_always": "Always allowed",
      "reject_once": "Denied",
      "reject_always": "Always denied",
      "cancelled": "Cancelled"
    },
    "answered": "Answered",
    "deciderLines": {
      "policy": "Decided by the new mode",
      "timeout": "No answer in time"
    },
    "modeSetTitle": "Mode set to {label}",
    "changedBy": "Changed{where}{who}",
    "vectors": {
      "resolutions": [
        {
          "optionId": "allow_once",
          "decidedBy": "user",
          "deciderName": "Robin Lee",
          "surface": "web",
          "line": "Allowed on web by Robin Lee"
        },
        {
          "optionId": "reject_once",
          "decidedBy": "user",
          "deciderName": "Sam Lee",
          "surface": "slack",
          "line": "Denied in Slack by Sam Lee"
        },
        {
          "optionId": "allow_always",
          "decidedBy": "user",
          "deciderName": null,
          "surface": null,
          "line": "Always allowed"
        },
        {
          "optionId": "allow_once",
          "decidedBy": "policy",
          "deciderName": null,
          "surface": null,
          "line": "Decided by the new mode"
        },
        {
          "optionId": "reject_once",
          "decidedBy": "timeout",
          "deciderName": null,
          "surface": null,
          "line": "No answer in time"
        },
        {
          "optionId": "something_new",
          "decidedBy": "user",
          "deciderName": "Dana",
          "surface": "slack",
          "line": "Answered in Slack by Dana"
        }
      ],
      "modeChanges": [
        {
          "mode": "bypass",
          "previousMode": "default",
          "changerName": "Robin Lee",
          "surface": "web",
          "presentation": {
            "title": "Mode set to Bypass permissions",
            "change": "Default → Bypass permissions",
            "attribution": "Changed on web by Robin Lee"
          }
        },
        {
          "mode": "plan",
          "previousMode": null,
          "changerName": "Sam Lee",
          "surface": "slack",
          "presentation": {
            "title": "Mode set to Plan",
            "change": "Plan",
            "attribution": "Changed in Slack by Sam Lee"
          }
        },
        {
          "mode": "read_only",
          "previousMode": "auto",
          "changerName": null,
          "surface": null,
          "presentation": {
            "title": "Mode set to Read-only",
            "change": "Auto → Read-only",
            "attribution": "Changed"
          }
        }
      ]
    }
  }
} as const;
