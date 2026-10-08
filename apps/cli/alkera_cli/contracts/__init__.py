"""Pure types shared across the CLI's packages.

A leaf package: its modules import nothing from ``alkera_cli`` outside
``alkera_cli.contracts``, so any layer may depend on them without pulling in
plugins, the harness or the daemon. Import the submodule that owns a type.
"""
