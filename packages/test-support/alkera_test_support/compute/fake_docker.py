"""An in-memory Docker for the ``localdev`` provider's tests.

It understands exactly the ``docker`` commands :mod:`alkera_core.compute.localdev`
sends and keeps the state they act on (images, containers, volumes), so a test
asserts what a developer would see in ``docker ps`` rather than which commands
were issued. Things outside the provider's vocabulary fail loudly. ``down``
makes every call fail the way a stopped Docker does.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from alkera_core.compute.localdev import DockerResult, DockerTimeoutError


@dataclass
class FakeContainer:
    name: str
    image: str
    status: str = "running"
    labels: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    env_file: str = ""
    volumes: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    privileged: bool = False
    restart: str = ""
    command: list[str] = field(default_factory=list)
    created: str = "2026-10-03T09:00:00.123456789Z"


@dataclass
class FakeDocker:
    images: set[str] = field(default_factory=set)
    containers: dict[str, FakeContainer] = field(default_factory=dict)
    volumes: set[str] = field(default_factory=set)
    builds: list[str] = field(default_factory=list)
    down: bool = False
    #: Verbs Docker carries out while the CLI gives up waiting on them, as a
    #: loaded Docker does.
    late: set[str] = field(default_factory=set)
    #: Verbs that hang outright: nothing happens and the CLI gives up.
    hung: set[str] = field(default_factory=set)

    async def __call__(self, args: Sequence[str], deadline_s: float) -> DockerResult:
        if self.down:
            return DockerResult(
                1, "", "Cannot connect to the Docker daemon. Is the docker daemon running?"
            )
        verb, *rest = list(args)
        if verb in self.hung:
            raise DockerTimeoutError(f"docker {verb} did not answer in {deadline_s:.0f}s")
        handler = getattr(self, f"_{verb}", None)
        if handler is None:
            raise AssertionError(f"the provider sent an unexpected docker command: {list(args)}")
        result: DockerResult = handler(rest)
        if verb in self.late:
            raise DockerTimeoutError(f"docker {verb} did not answer in {deadline_s:.0f}s")
        return result

    @staticmethod
    def _missing(name: str) -> DockerResult:
        return DockerResult(1, "", f"Error response from daemon: No such object: {name}")

    def _info(self, rest: list[str]) -> DockerResult:
        return DockerResult(0, "27.0.0\n", "")

    def _image(self, rest: list[str]) -> DockerResult:
        assert rest[0] == "inspect", rest
        return DockerResult(0, "[]", "") if rest[1] in self.images else self._missing(rest[1])

    def _build(self, rest: list[str]) -> DockerResult:
        tag = rest[rest.index("-t") + 1]
        self.images.add(tag)
        self.builds.append(tag)
        return DockerResult(0, "", "")

    def _inspect(self, rest: list[str]) -> DockerResult:
        if rest[0] == "--format":
            box = self.containers.get(rest[2])
            return DockerResult(0, f"{box.status}\n", "") if box else self._missing(rest[2])
        rows: list[dict[str, Any]] = []
        for ident in rest:
            box = self.containers.get(ident)
            if box is None:
                return self._missing(ident)
            rows.append(
                {
                    "Name": f"/{box.name}",
                    "Created": box.created,
                    "State": {"Status": box.status},
                    "Config": {"Labels": dict(box.labels), "Image": box.image},
                }
            )
        return DockerResult(0, json.dumps(rows), "")

    def _ps(self, rest: list[str]) -> DockerResult:
        wanted = [rest[i + 1].removeprefix("label=") for i, a in enumerate(rest) if a == "--filter"]
        names = [
            box.name
            for box in self.containers.values()
            if all(box.labels.get(k) == v for k, v in (w.split("=", 1) for w in wanted))
        ]
        return DockerResult(0, "".join(f"{n}\n" for n in names), "")

    def _run(self, rest: list[str]) -> DockerResult:
        box = FakeContainer(name="", image="")
        i = 0
        while i < len(rest):
            arg = rest[i]
            if arg in ("--detach",):
                i += 1
                continue
            if arg == "--privileged":
                box.privileged = True
                i += 1
                continue
            if not arg.startswith("--"):
                box.image, box.command = arg, rest[i + 1 :]
                break
            value = rest[i + 1]
            if arg == "--name":
                box.name = value
            elif arg == "--label":
                key, _, val = value.partition("=")
                box.labels[key] = val
            elif arg == "--env":
                key, _, val = value.partition("=")
                box.env[key] = val
            elif arg == "--env-file":
                box.env_file = value
            elif arg == "--volume":
                box.volumes.append(value)
                source = value.split(":", 1)[0]
                if not source.startswith("/"):
                    self.volumes.add(source)
            elif arg == "--add-host":
                box.hosts.append(value)
            elif arg == "--restart":
                box.restart = value
            i += 2
        if box.name in self.containers:
            return DockerResult(
                125, "", f'Conflict. The container name "/{box.name}" is already in use'
            )
        if box.image not in self.images:
            return DockerResult(125, "", f"Unable to find image '{box.image}' locally")
        self.containers[box.name] = box
        return DockerResult(0, f"{box.name}\n", "")

    def _start(self, rest: list[str]) -> DockerResult:
        box = self.containers.get(rest[0])
        if box is None:
            return self._missing(rest[0])
        box.status = "running"
        return DockerResult(0, f"{rest[0]}\n", "")

    def _stop(self, rest: list[str]) -> DockerResult:
        box = self.containers.get(rest[-1])
        if box is None:
            return self._missing(rest[-1])
        box.status = "exited"
        return DockerResult(0, f"{rest[-1]}\n", "")

    def _rm(self, rest: list[str]) -> DockerResult:
        name = rest[-1]
        if self.containers.pop(name, None) is None:
            return self._missing(name)
        return DockerResult(0, f"{name}\n", "")

    def _volume(self, rest: list[str]) -> DockerResult:
        assert rest[0] == "rm", rest
        self.volumes.discard(rest[-1])
        return DockerResult(0, "", "")


__all__ = ["FakeContainer", "FakeDocker"]
