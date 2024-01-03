import atexit
import os
import signal
from multiprocessing import Queue, get_context
from threading import RLock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trytond.pool import Pool

    from .pool import Dependencies, ModelName, ModuleName, PoolKind


class CompanionUnexepectedReturnError(Exception):
    pass


class CompanionCrashedError(Exception):
    pass


class CompanionKilledError(Exception):
    pass


COMPANION_READY = "companion_ready"
COMPANION_CRASHED = "companion_crashed"
COMPANION_UNEXPECTED_COMMAND = "companion_unexpected_command"
COMPANION_RESULT_OK = "companion_result_ok"
COMPANION_COMMAND_INIT_POOL = "companion_init_pool"
COMPANION_COMMAND_GET_MODEL = "companion_get_model"
COMPANION_COMMAND_GET_SUPER_CALLS = "companion_get_super_calls"
COMPANION_COMMAND_GET_COMPLETIONS = "companion_get_completions"
COMPANION_COMMAND_MODULE_INFO = "companion_get_module_info"


def kill_child(pid: int | None) -> None:
    if pid is not None:
        os.kill(pid, signal.SIGKILL)


class Companion:
    def __init__(self) -> None:
        super().__init__()
        self._lock = RLock()
        context = get_context("spawn")
        self._queue_in: Queue = context.Queue()
        self._queue_out: Queue = context.Queue()
        self._process = context.Process(
            target=main, args=[self._queue_out, self._queue_in]
        )
        self._process.start()
        # Keep a reference to the killer function to be able to unregister it
        self._killer = lambda: kill_child(self._process.pid)
        atexit.register(self._killer)

    def close(self) -> None:
        with self._lock:
            if self._process:
                self._process.kill()
                atexit.unregister(self._killer)

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def _call(
        self,
        command: str,
        parameters: tuple,
    ) -> Any:
        """
        Calls the companion process for informations
        """
        # Since we are using a queuing mechanism, we have to lock to avoid
        # wrong call orders
        with self._lock:
            self._queue_out.put((command, parameters))
            return_value, result = self._queue_in.get()
            if return_value == COMPANION_CRASHED:
                raise CompanionCrashedError(result)
            return result

    def get_module_info(self, module_name: "ModuleName") -> dict:
        return self._call(COMPANION_COMMAND_MODULE_INFO, (module_name,))

    def init_pool(self, key: "Dependencies") -> dict:
        return self._call(
            COMPANION_COMMAND_INIT_POOL,
            (key,),
        )

    def fetch_model(
        self, key: "Dependencies", name: "ModelName", kind: "PoolKind"
    ) -> dict:
        return self._call(COMPANION_COMMAND_GET_MODEL, (key, name, kind))

    def fetch_super_information(
        self,
        key: "Dependencies",
        kind: "PoolKind",
        name: "ModelName",
        function_name: str,
    ) -> Any:
        return self._call(
            COMPANION_COMMAND_GET_SUPER_CALLS, (key, kind, name, function_name)
        )

    def fetch_completions(
        self, key: "Dependencies", name: "ModelName", kind: "PoolKind"
    ) -> dict[str, dict[str, Any]]:
        return self._call(COMPANION_COMMAND_GET_COMPLETIONS, (key, name, kind))


def main(queue_in: Queue, queue_out: Queue) -> None:
    """
    The main function of the spawned process. It basically initialize the
    tryton Pool, then wait for instructions.

    Any error leads to the end of the process
    """
    import os
    import traceback

    try:
        os.environ["TRYTON_ANALYZER_RUNNING"] = "1"
        pools: dict["Dependencies", "Pool"] = _init()
        while True:
            instruction, parameters = queue_in.get()
            if instruction == COMPANION_COMMAND_INIT_POOL:
                queue_out.put(
                    (COMPANION_RESULT_OK, _init_pool(parameters, pools))
                )
            elif instruction == COMPANION_COMMAND_MODULE_INFO:
                from trytond.modules import get_module_info

                queue_out.put(
                    (COMPANION_RESULT_OK, get_module_info(parameters[0]))
                )
            elif instruction == COMPANION_COMMAND_GET_MODEL:
                queue_out.put(
                    (COMPANION_RESULT_OK, _get_model(parameters, pools))
                )
            elif instruction == COMPANION_COMMAND_GET_SUPER_CALLS:
                queue_out.put(
                    (COMPANION_RESULT_OK, _get_super_calls(parameters, pools))
                )
            elif instruction == COMPANION_COMMAND_GET_COMPLETIONS:
                queue_out.put(
                    (COMPANION_RESULT_OK, _get_completions(parameters, pools))
                )
            else:
                queue_out.put((COMPANION_UNEXPECTED_COMMAND, instruction))
    except Exception as e:
        queue_out.put((COMPANION_CRASHED, traceback.print_exception(e)))


def _init() -> dict["Dependencies", "Pool"]:
    """Initializes the Tryton Pool"""
    from trytond.pool import Pool

    pools: dict["Dependencies", Pool] = {}
    pool = Pool(module_list=("ir", "res"))
    pool.init()
    Pool._current = None
    pools[("ir", "res")] = pool
    return pools


def _init_pool(
    parameters: list[Any], pools: dict["Dependencies", "Pool"]
) -> dict[str, list[str]]:
    """
    Look for an existing pool matching the requested dependencies, and init a
    new one if None is found
    """
    from trytond.pool import Pool

    (key,) = parameters
    if key not in pools:
        pool = Pool(module_list=key)
        pool.init()
        Pool._current = None
        pools[key] = pool
    return {
        "models": [x for x, _ in pools[key].iterobject(type="model")],
        "wizards": [x for x, _ in pools[key].iterobject(type="wizard")],
    }


def _get_model(
    parameters: list[Any], pools: dict["Dependencies", "Pool"]
) -> dict[str, dict[str, Any] | set]:
    """
    Fetches detailed informations about a model
    """
    from trytond.wizard import StateView

    key, name, kind = parameters
    # assert key in pools
    pool = pools[key]
    model = pool.get(name, type=kind)
    result: dict[str, dict[str, Any] | set] = {"attrs": set(dir(model))}
    if kind == "model":
        fields = {}
        for fname, field in model._fields.items():
            fields[fname] = {
                "string": field.string,
                "type": field._type,
            }
            if field._type in ("many2one", "one2many"):
                fields[fname]["relation"] = field.model_name
            elif field._type == "many2many":
                if field.target:
                    fields[fname]["relation"] = (
                        pool.get(field.relation_name)
                        ._fields[field.target]
                        .model_name
                    )
                else:
                    fields[fname]["relation"] = field.relation_name
        result["fields"] = fields
    elif kind == "wizard":
        states = {}
        for state_name, state in model.states.items():
            if isinstance(state, StateView):
                states[state_name] = {
                    "relation": state.model_name,
                }
        result["states"] = states
    return result


def _get_super_calls(
    parameters: list[Any], pools: dict["Dependencies", "Pool"]
) -> list[Any]:
    """
    Fetch detailed informations about a function
    """
    key, kind, name, func_name = parameters
    model = pools[key].get(name, type=kind)
    result: list[tuple[str, str | None | tuple[int, int]]] = []
    for klass in model.mro():
        if func_name not in dir(klass):
            result.append((str(klass), None))
            continue
        parent_func = getattr(klass, func_name)
        code_object = getattr(parent_func, "__code__", None)
        if code_object is None:
            result.append((str(klass), "no_code"))
            continue
        result.append(
            (
                str(klass),
                (
                    code_object.co_filename,
                    code_object.co_firstlineno,
                ),
            )
        )
    return result


def _get_completions(
    parameters: list[Any], pools: dict["Dependencies", "Pool"]
) -> dict[str, dict[str, Any]]:
    """
    Returns possible completions for a given model
    """
    from trytond.model import fields

    key, name, kind = parameters
    model = pools[key].get(name, type=kind)
    result = {}
    for elem in dir(model):
        if kind == "model" and elem in model._fields:
            field = model._fields[elem]
            field_info = {
                "type": "field",
                "class_name": str(field.__class__),
                "string": field.string,
            }
            if isinstance(field, fields.Function):
                field_info["function"] = True
            if field.domain:
                field_info["domain"] = str(field.domain)
            if field.states:
                field_info["states"] = str(field.states)
            if isinstance(field, fields.Selection):
                if isinstance(field.selection, (tuple, list)):
                    field_info["selection"] = list(field.selection)
            result[elem] = field_info
        elif kind == "wizard" and elem in model._states:
            result[elem] = {
                "type": "state",
                "class_name": str(model._states[elem].__class__),
            }
        else:
            try:
                documentation = getattr(model, elem).__doc__
            except NotImplementedError:
                documentation = ""
            result[elem] = {
                "type": "method",
                "documentation": documentation,
            }
    return result
