"""Notebooks on the platform: the wire shapes of the notebook routes and the
realtime notebook channel, and the tables that record runs, kernels and edits.

The live document itself is a Loro document of type ``notebook`` on the CRDT
lane (``backend.services.crdt``); the file at rest is the ``.alknb.py`` node on
the drive. Nothing here parses the file: the format code runs in the CRDT
sandbox workers.
"""
