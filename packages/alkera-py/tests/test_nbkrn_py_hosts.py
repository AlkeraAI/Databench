"""Host selection and the hosts' own behavior, in plain Python.

The real runtimes are covered elsewhere (a subprocess kernel, stock marimo,
an IPython shell); these pin how ``alkera._host.current()`` chooses among
them from what ``sys.modules`` holds, and what each host does on its own."""

from __future__ import annotations

import base64
import importlib.abc
import sys
import types
from collections.abc import Iterator, Sequence
from importlib.machinery import ModuleSpec
from typing import Any

import alkera
import pytest
from alkera import _host
from alkera.errors import FeatureMissing, ServiceUnavailable, StopCell


class FakeRuntime:
    """The kernel's runtime object as host protocol 1 describes it."""

    protocol_version = 1
    name = "kernel-object"

    def __init__(self) -> None:
        self.shown: list[tuple[str, Any]] = []
        self.reactive: list[Any] = []
        self.data_dir = "/data"
        self.arguments: dict[str, Any] = {"k": "v"}

    def display(self, obj: Any) -> None:
        self.shown.append(("append", obj))

    def replace(self, obj: Any) -> None:
        self.shown.append(("replace", obj))

    def clear(self) -> None:
        self.shown.append(("clear", None))

    def format(self, obj: Any) -> dict[str, Any]:
        return {"text/plain": f"formatted {obj!r}"}

    def register_interruptible(self, obj: Any) -> None:
        pass

    def unregister_interruptible(self, obj: Any) -> None:
        pass

    def register_reactive(self, obj: Any) -> None:
        self.reactive.append(obj)

    def run_context(self) -> dict[str, Any] | None:
        return {"run_id": "r1", "cell_id": "c1"}

    def call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> Any:
        return {"method": method, "params": params}

    def args(self) -> dict[str, Any]:
        return self.arguments

    def stop_exception(self) -> type[BaseException]:
        return KernelStop


class KernelStop(Exception):  # noqa: N818 - mirrors the kernel's StopCell
    _alkera_stop = True


def _install_runtime(monkeypatch: pytest.MonkeyPatch, runtime: object) -> None:
    module = types.ModuleType("_alkera_runtime")
    module.host = runtime  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_alkera_runtime", module)


@pytest.fixture(autouse=True)
def _no_ambient_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_alkera_runtime", "marimo", "IPython"):
        monkeypatch.delitem(sys.modules, name, raising=False)


# --------------------------------------------------------------------------- selection


def test_plain_python_gets_the_script_host() -> None:
    assert isinstance(_host.current(), _host.ScriptHost)
    assert _host.current().name == "script"


def test_a_protocol_1_runtime_is_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = FakeRuntime()
    _install_runtime(monkeypatch, runtime)
    host = _host.current()
    assert isinstance(host, _host.RuntimeHost)
    assert host.runtime is runtime
    assert host is _host.current()
    alkera.output.append("a")
    alkera.output.replace("b")
    alkera.output.clear()
    assert runtime.shown == [("append", "a"), ("replace", "b"), ("clear", None)]


def test_a_replaced_runtime_object_is_adopted_afresh(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runtime(monkeypatch, FakeRuntime())
    first = _host.current()
    second_runtime = FakeRuntime()
    _install_runtime(monkeypatch, second_runtime)
    second = _host.current()
    assert first is not second
    assert isinstance(second, _host.RuntimeHost) and second.runtime is second_runtime


@pytest.mark.parametrize(
    "version",
    [pytest.param(2, id="newer"), pytest.param(0, id="older"), pytest.param("1", id="string")],
)
def test_a_runtime_speaking_another_protocol_is_not_adopted(
    monkeypatch: pytest.MonkeyPatch, version: object
) -> None:
    runtime = FakeRuntime()
    runtime.protocol_version = version  # type: ignore[assignment]
    _install_runtime(monkeypatch, runtime)
    assert isinstance(_host.current(), _host.ScriptHost)


def test_a_runtime_without_display_is_not_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = FakeRuntime()
    runtime.display = None  # type: ignore[assignment,method-assign]
    _install_runtime(monkeypatch, runtime)
    assert isinstance(_host.current(), _host.ScriptHost)
    with pytest.raises(TypeError, match="display"):
        _host.RuntimeHost(runtime)


class PartialRuntime:
    """A kernel object from a release with fewer methods than this one."""

    protocol_version = 1

    def __init__(self) -> None:
        self.shown: list[Any] = []

    def display(self, obj: Any) -> None:
        self.shown.append(obj)


def test_methods_a_runtime_lacks_behave_as_in_plain_python(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = PartialRuntime()
    _install_runtime(monkeypatch, runtime)
    host = _host.current()
    assert isinstance(host, _host.RuntimeHost)
    alkera.output.append(alkera.md("kept"))
    assert len(runtime.shown) == 1
    alkera.output.replace(alkera.md("printed"))
    assert capsys.readouterr().out == "printed\n"
    assert host.settings() == {}
    assert dict(alkera.args()) == {}
    assert host.format(2)["text/plain"] == "2"
    with pytest.raises(ServiceUnavailable):
        alkera.call("x")
    with pytest.raises(StopCell):
        alkera.stop(True)
    assert getattr(host, "open_comm", None) is None


def test_settings_are_forwarded_and_args_read_from_them(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = FakeRuntime()
    runtime.settings = lambda: {"dataframe": "polars", "args": {"from": "settings"}}  # type: ignore[attr-defined]
    _install_runtime(monkeypatch, runtime)
    host = _host.current()
    assert host.settings() == {"dataframe": "polars", "args": {"from": "settings"}}
    assert dict(alkera.args()) == {"from": "settings"}


def test_args_fall_back_to_the_runtimes_args_without_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runtime(monkeypatch, FakeRuntime())
    assert dict(alkera.args()) == {"k": "v"}


def test_open_comm_is_the_runtimes_own_and_absent_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert getattr(_host.current(), "open_comm", None) is None
    runtime = FakeRuntime()
    opened: list[str] = []
    runtime.open_comm = lambda target, data, metadata, on_msg: opened.append(target) or "comm"  # type: ignore[attr-defined]
    _install_runtime(monkeypatch, runtime)
    host = _host.current()
    open_comm = getattr(host, "open_comm", None)
    assert open_comm is not None
    assert open_comm("alkera.ui", {}, {}, lambda msg: None) == "comm"
    assert opened == ["alkera.ui"]


def test_only_public_runtime_attributes_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = FakeRuntime()
    runtime._secret = 1  # type: ignore[attr-defined]
    runtime.codecs = lambda: ["json"]  # type: ignore[attr-defined]
    _install_runtime(monkeypatch, runtime)
    host = _host.current()
    assert host.codecs() == ["json"]  # type: ignore[attr-defined]
    assert not hasattr(host, "_secret")
    with pytest.raises(AttributeError, match="no nothing_here"):
        host.nothing_here  # type: ignore[attr-defined]  # noqa: B018


def test_a_runtime_module_without_a_host_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "_alkera_runtime", types.ModuleType("_alkera_runtime"))
    assert isinstance(_host.current(), _host.ScriptHost)


def _fake_marimo(monkeypatch: pytest.MonkeyPatch, *, running: bool) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []
    marimo = types.ModuleType("marimo")
    marimo.output = types.SimpleNamespace(  # type: ignore[attr-defined]
        append=lambda obj: calls.append(("append", obj)),
        replace=lambda obj: calls.append(("replace", obj)),
        clear=lambda: calls.append(("clear", None)),
    )

    class MarimoStopError(BaseException):
        def __init__(self, output: object | None) -> None:
            self.output = output

    marimo.MarimoStopError = MarimoStopError  # type: ignore[attr-defined]
    marimo.cli_args = lambda: {"n": 3}  # type: ignore[attr-defined]
    marimo.as_html = lambda obj: types.SimpleNamespace(text=f"<i>{obj}</i>")  # type: ignore[attr-defined]
    context = types.ModuleType("marimo._runtime.context.types")

    def get_context() -> object:
        if not running:
            raise LookupError("no context")
        return object()

    context.get_context = get_context  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "marimo", marimo)
    monkeypatch.setitem(sys.modules, "marimo._runtime.context.types", context)
    return calls


def test_imported_but_not_running_marimo_is_plain_python(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_marimo(monkeypatch, running=False)
    assert isinstance(_host.current(), _host.ScriptHost)


def test_running_marimo_gets_the_marimo_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_marimo(monkeypatch, running=True)
    host = _host.current()
    assert isinstance(host, _host.MarimoHost)
    alkera.output.append("a")
    alkera.output.replace("b")
    alkera.output.clear()
    assert calls == [("append", "a"), ("replace", "b"), ("clear", None)]
    assert dict(alkera.args()) == {"n": 3}
    assert host.format(5)["text/html"] == "<i>5</i>"
    # An alkera output keeps its own HTML rather than marimo's generic one.
    assert host.format(alkera.md("*x*"))["text/html"] == alkera.md("*x*")._mime_()[1]


@pytest.mark.parametrize(
    "output", [pytest.param(alkera.md("wait"), id="with-output"), pytest.param(None, id="bare")]
)
def test_marimo_stop_hands_the_output_to_marimos_stop(
    monkeypatch: pytest.MonkeyPatch, output: object | None
) -> None:
    calls = _fake_marimo(monkeypatch, running=True)
    stop_error = sys.modules["marimo"].MarimoStopError
    with pytest.raises(stop_error) as info:
        alkera.stop(True, output)
    # marimo shows a stop's own output; appending it first would read as a failure.
    assert calls == []
    assert info.value.output is output
    assert getattr(type(info.value), "_alkera_stop", False) is True


def test_runtime_wins_over_marimo(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_marimo(monkeypatch, running=True)
    _install_runtime(monkeypatch, FakeRuntime())
    assert isinstance(_host.current(), _host.RuntimeHost)


def _fake_ipython(
    monkeypatch: pytest.MonkeyPatch, *, shell: object | None
) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []
    ipython = types.ModuleType("IPython")
    ipython.get_ipython = lambda: shell  # type: ignore[attr-defined]
    display = types.ModuleType("IPython.display")
    display.display = lambda obj: calls.append(("display", obj))  # type: ignore[attr-defined]
    display.clear_output = lambda wait=False: calls.append(("clear", wait))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "IPython", ipython)
    monkeypatch.setitem(sys.modules, "IPython.display", display)
    return calls


def test_an_ipython_shell_gets_the_jupyter_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_ipython(monkeypatch, shell=object())
    assert isinstance(_host.current(), _host.JupyterHost)
    alkera.output.append("a")
    alkera.output.replace("b")
    alkera.output.clear()
    assert calls == [("display", "a"), ("clear", True), ("display", "b"), ("clear", False)]


def test_ipython_imported_without_a_shell_is_plain_python(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ipython(monkeypatch, shell=None)
    assert isinstance(_host.current(), _host.ScriptHost)


def test_marimo_wins_over_ipython(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ipython(monkeypatch, shell=object())
    _fake_marimo(monkeypatch, running=True)
    assert isinstance(_host.current(), _host.MarimoHost)


def test_use_overrides_and_nests() -> None:
    outer, inner = _host.ScriptHost(), _host.ScriptHost()
    with _host.use(outer):
        assert _host.current() is outer
        with _host.use(inner):
            assert _host.current() is inner
        assert _host.current() is outer
    assert _host.current() is not outer


# --------------------------------------------------------------------------- the script host


def test_script_host_prints_markdown_when_there_is_some(capsys: pytest.CaptureFixture[str]) -> None:
    alkera.output.append(alkera.md("**x**"))
    alkera.output.replace(alkera.html("<b>y</b>"))
    alkera.output.append(41 + 1)
    alkera.output.clear()
    assert capsys.readouterr().out == "**x**\ny\n42\n"


def test_script_and_marimo_hosts_have_no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _host.current().settings() == {}
    _fake_marimo(monkeypatch, running=True)
    assert _host.current().settings() == {}
    assert getattr(_host.current(), "open_comm", None) is None


def test_script_host_has_no_service() -> None:
    with pytest.raises(ServiceUnavailable, match=r"alkera.call\('jobs.start'\)"):
        alkera.call("jobs.start", n=1)
    host = _host.current()
    assert host.run_context() is None
    assert host.data_dir is None
    assert dict(alkera.args()) == {}


def test_interruptibles_are_held_weakly_where_possible() -> None:
    class Query:
        def interrupt(self) -> None:
            pass

    host = _host.ScriptHost()
    query = Query()
    unhashable: list[int] = []
    host.register_interruptible(query)
    host.register_interruptible(unhashable)
    host.register_interruptible(unhashable)
    assert host.interruptibles == [query, unhashable]
    del query
    assert host.interruptibles == [unhashable]
    host.unregister_interruptible(unhashable)
    assert host.interruptibles == []


# --------------------------------------------------------------------------- the public API


def test_stop_with_a_false_predicate_does_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    alkera.stop(False, alkera.md("never"))
    alkera.stop([], alkera.md("never"))
    assert capsys.readouterr().out == ""


def test_stop_shows_its_output_then_raises(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(StopCell) as info:
        alkera.stop(True, alkera.md("*halt*"))
    assert capsys.readouterr().out == "*halt*\n"
    assert type(info.value)._alkera_stop is True
    assert alkera.StopCell is StopCell


def test_stop_under_the_runtime_raises_the_kernels_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    _install_runtime(monkeypatch, runtime)
    with pytest.raises(KernelStop):
        alkera.stop(True)
    assert runtime.shown == []


def test_a_runtime_returning_no_exception_class_falls_back_to_stopcell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeRuntime()
    runtime.stop_exception = lambda: "nonsense"  # type: ignore[assignment,return-value]
    _install_runtime(monkeypatch, runtime)
    with pytest.raises(StopCell):
        alkera.stop(True)


def test_args_is_a_read_only_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = FakeRuntime()
    _install_runtime(monkeypatch, runtime)
    args = alkera.args()
    with pytest.raises(TypeError):
        args["k"] = "changed"  # type: ignore[index]
    runtime.arguments["k"] = "later"
    assert args["k"] == "v"


def test_call_and_widget_go_through_the_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = FakeRuntime()
    _install_runtime(monkeypatch, runtime)
    assert alkera.call("svc.method", a=1) == {"method": "svc.method", "params": {"a": 1}}
    model = object()
    assert alkera.widget(model) is model
    assert runtime.reactive == [model]
    host = _host.current()
    assert host.data_dir == "/data"
    assert host.run_context() == {"run_id": "r1", "cell_id": "c1"}
    assert host.format(1) == {"text/plain": "formatted 1"}


# --------------------------------------------------------------------------- formatting


class _Png:
    def _repr_png_(self) -> bytes:
        return b"\x89PNG"


class _BundleTuple:
    def _repr_mimebundle_(self, include: Any = None, exclude: Any = None) -> Any:
        return ({"text/html": "<b>t</b>", "text/plain": None}, {"metadata": 1})


class _Declines:
    def _repr_html_(self) -> str:
        raise NotImplementedError

    def _repr_markdown_(self) -> str:
        return "*m*"


@pytest.mark.parametrize(
    ("obj", "expected"),
    [
        pytest.param("text", {"text/plain": "text"}, id="str-as-is"),
        pytest.param(3, {"text/plain": "3"}, id="repr"),
        pytest.param(_Png(), {"image/png": base64.b64encode(b"\x89PNG").decode()}, id="png-base64"),
        pytest.param(_BundleTuple(), {"text/html": "<b>t</b>"}, id="bundle-tuple-drops-none"),
        pytest.param(_Declines(), {"text/markdown": "*m*"}, id="not-implemented-skipped"),
        pytest.param(alkera.md("a"), {"text/markdown": "a", "text/plain": "a"}, id="alkera-output"),
    ],
)
def test_format_value(obj: object, expected: dict[str, Any]) -> None:
    bundle = _host.format_value(obj)
    for mime, value in expected.items():
        assert bundle[mime] == value
    assert "text/plain" in bundle


def test_format_value_does_not_call_methods_on_a_class() -> None:
    bundle = _host.format_value(_Png)
    assert set(bundle) == {"text/plain"}


# --------------------------------------------------------------------------- lazy parts


class _RaisingFinder(importlib.abc.MetaPathFinder):
    def __init__(self, missing: str) -> None:
        self.missing = missing

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: types.ModuleType | None = None
    ) -> ModuleSpec | None:
        if fullname == "alkera.chart":
            raise ModuleNotFoundError(f"No module named {self.missing!r}", name=self.missing)
        return None


@pytest.fixture
def _forget_lazy(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The chart part ships in this tree, so another test may already have
    # imported it; hide it so the lazy loader runs again.
    for module in [m for m in sys.modules if m == "alkera.chart" or m.startswith("alkera.chart.")]:
        monkeypatch.delitem(sys.modules, module)
    for name in ("chart", "ui", "sql"):
        monkeypatch.delitem(alkera.__dict__, name, raising=False)
    yield
    for name in ("chart", "ui", "sql"):
        alkera.__dict__.pop(name, None)


@pytest.mark.usefixtures("_forget_lazy")
def test_a_missing_part_raises_a_clear_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "meta_path", [_RaisingFinder("alkera.chart"), *sys.meta_path])
    with pytest.raises(
        FeatureMissing, match=r"alkera\.chart is not part of this alkera installation"
    ):
        alkera.chart  # noqa: B018
    with pytest.raises(ImportError):
        from alkera import chart  # noqa: F401


@pytest.mark.usefixtures("_forget_lazy")
def test_a_present_part_is_imported_on_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    sql_module = types.ModuleType("alkera._sql")
    sql_module.sql = lambda query: f"ran {query}"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alkera._sql", sql_module)
    assert alkera.sql("select 1") == "ran select 1"


@pytest.mark.usefixtures("_forget_lazy")
def test_a_part_whose_own_dependency_is_missing_is_not_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "meta_path", [_RaisingFinder("numpy"), *sys.meta_path])
    with pytest.raises(ModuleNotFoundError) as info:
        alkera.chart  # noqa: B018
    assert info.value.name == "numpy"


def test_an_unknown_attribute_is_an_attribute_error() -> None:
    assert not hasattr(alkera, "no_such_call")


def test_the_data_test_api_is_reexported_unchanged() -> None:
    from alkera import testing

    for name in (
        "Connection",
        "RegisteredTest",
        "RowSet",
        "registered_tests",
        "reset_registered",
        "test",
    ):
        assert getattr(alkera, name) is getattr(testing, name)
    assert alkera.__test__ is False
    assert testing.__test__ is False
    assert testing.RegisteredTest.__test__ is False
