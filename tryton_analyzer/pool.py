from __future__ import annotations

import ast
import importlib
import os
from collections.abc import Generator
from enum import StrEnum
from io import BytesIO
from pathlib import Path
from typing import Any, cast

from libcst.metadata import CodeRange
from lsprotocol.types import CompletionItem, CompletionItemKind
from lxml import etree

from .analyzer import CompletionTargetFoundError, PythonCompletioner
from .parsing import (
    ParsedFile,
    ParsedPythonFile,
    ParsedViewFile,
    ParsedXMLFile,
    ParsingError,
)
from .pool_companion import Companion
from .tools import Diagnostic

Dependencies = tuple[str, ...]
ModuleName = str
ModelName = str


class PoolKind(StrEnum):
    MODEL = "model"
    WIZARD = "wizard"


class UnknownModelException(KeyError):
    pass


class PoolManager:
    def __init__(self) -> None:
        super().__init__()
        self._parsed: dict[Path, ParsedFile | None] = {}
        self._modules: dict[ModuleName, Module] = {}
        self._pools: dict[Dependencies, Pool] = {}
        self._companion: Companion = Companion()

    def close(self) -> None:
        self._companion.close()

    def get_companion(self) -> Companion:
        """
        Returns a living companion, removing the current one if it is not alive
        """
        if self._companion.is_alive():
            return self._companion
        new_companion = Companion()
        dead_companion = self._companion
        self._companion = new_companion
        self._pools.clear()
        dead_companion.close()
        return new_companion

    def is_alive(self) -> bool:
        return self._companion.is_alive()

    def get_pool(self, module_names: Dependencies) -> Pool:
        """
        Returns a proxy to the tryton pool for the requested dependencies,
        which can later be requested for informations
        """
        key = tuple(sorted(module_names))
        if key in self._pools:
            return self._pools[key]

        companion = self.get_companion()
        result = companion.init_pool(key)
        pool = Pool(self, key, result["models"], result["wizards"])
        self._pools[key] = pool
        return self._pools[key]

    def fetch_model(
        self, key: Dependencies, name: ModelName, kind: PoolKind
    ) -> dict:
        """
        Load informations of a model from the pool
        """
        return self.get_companion().fetch_model(key, name, kind)

    def fetch_super_information(
        self,
        key: Dependencies,
        kind: PoolKind,
        name: ModelName,
        function_name: str,
    ) -> Any:
        """
        Returns a method's parents in the MRO
        """
        return self.get_companion().fetch_super_information(
            key, kind, name, function_name
        )

    def fetch_completions(
        self, key: Dependencies, name: ModelName, kind: PoolKind
    ) -> dict[str, dict[str, Any]]:
        """
        Returns completion informations for a model
        """
        return self.get_companion().fetch_completions(key, name, kind)

    def _get_module(self, module_name: ModuleName) -> Module:
        """
        Get informations about a module, loading it if needed
        """
        if module_name in self._modules:
            return self._modules[module_name]

        companion = self.get_companion()
        result = companion.get_module_info(module_name)
        module = Module(self, module_name, result)
        self._modules[module_name] = module
        return module

    def generate_diagnostics(
        self,
        path: Path,
        ranges: list[CodeRange] | None = None,
        data: str | None = None,
    ) -> list[Diagnostic]:
        """
        Generate diagnostics for a given path
        """
        parsed = self.get_parsed(path, data=data)
        if parsed:
            return self._get_diagnostics(parsed, ranges=ranges)
        return []

    def generate_completions(
        self, path: Path, line: int, column: int, data: str | None = None
    ) -> list[CompletionItem]:
        """
        Find completions for the provided position
        """
        parsed = self.get_parsed(path, data=data)
        if parsed and isinstance(parsed, ParsedPythonFile):
            return self._get_completions(parsed, line, column)
        return []

    def get_parsed(
        self, path: Path, data: str | None = None
    ) -> ParsedFile | None:
        """
        Returns a parsed file from the provided path / data, selecting the
        appropriate parser.
        """
        parser_class: type[ParsedFile] | None = self._parser_from_path(path)
        if parser_class is None:
            return None
        can_fallback = data is not None
        if not can_fallback:
            with open(path) as f:
                data = f.read()

        parsed: ParsedFile | None = None
        try:
            parsed = parser_class(path, data=data)
        except ParsingError:
            if path in self._parsed:
                # Reuse the previously parsed informations
                parsed = self._parsed[path]
            if parsed is None and can_fallback:
                # If the provided data cannot be parsed, try to parse the
                # underlying file, even though it may be incomplete
                try:
                    parsed = parser_class(path)
                except ParsingError:
                    parsed = None
            else:
                parsed = None

        if parsed is not None:
            module_name: ModuleName | None = parsed.get_module_name()
            if module_name:
                parsed.set_module(self._get_module(module_name))
        self._parsed[path] = parsed
        return parsed

    def _parser_from_path(self, path: Path) -> type[ParsedFile] | None:
        """
        Selects the right parser based on the path
        """
        if path.match("*.py"):
            return ParsedPythonFile
        elif path.match("view/*.xml"):
            return ParsedViewFile
        elif path.match("*.xml"):
            return ParsedXMLFile
        else:
            return None

    def _get_diagnostics(
        self, parsed: ParsedFile, ranges: list[CodeRange] | None = None
    ) -> list[Diagnostic]:
        return parsed.get_analyzer(self).analyze(ranges=ranges or [])

    def _get_completions(
        self, parsed: ParsedPythonFile, line: int, column: int
    ) -> list[CompletionItem]:
        # For completions, we use a variant of the diagnostic analyzer which
        # will ignore classes / functions other than where the completion is
        # requested. Once the completion point is found, it will raise a
        # CompletionTargetFoundError, with the identified model at this point.
        # We then intercept the error to propose completions
        completioner = PythonCompletioner(parsed, self, line, column)
        try:
            completioner.analyze()
        except CompletionTargetFoundError:
            if completioner._target_model:
                return completioner._target_model.generate_completions()
        return []

    def generate_module_diagnostics(
        self, module_name: ModuleName
    ) -> list[Diagnostic]:
        """
        Shortcut to get diagnostics for all files of a module
        """
        module = self._get_module(module_name)
        module_path = module.get_directory()
        diagnostics = []

        def to_analyze() -> Generator[Path, None, None]:
            for file_path in os.listdir(module_path):
                yield Path(file_path)
            if os.path.isdir(module_path / "tests"):
                for file_path in os.listdir(module_path / "tests"):
                    yield Path("tests") / file_path
            if os.path.isdir(module_path / "view"):
                for file_path in os.listdir(module_path / "view"):
                    if file_path.endswith(".xml"):
                        yield Path("view") / file_path

        for file_path in to_analyze():
            diagnostics += self.generate_diagnostics(module_path / file_path)

        return diagnostics


class Pool:
    """
    The pool proxy for a set of dependencies.

    Its main usage is for caching some informations, as well as an entry point
    to request model informations
    """

    supported_keys = {"model", "wizard"}

    def __init__(
        self,
        manager: PoolManager,
        key: Dependencies,
        models: list[ModelName],
        wizards: list[ModelName],
    ) -> None:
        super().__init__()
        self._key: Dependencies = key
        self._manager: PoolManager = manager
        self.models: dict[ModelName, PoolModel | None] = {
            x: None for x in models
        }
        self.wizards: dict[ModelName, PoolModel | None] = {
            x: None for x in wizards
        }

    def get(
        self, name: ModelName, kind: PoolKind = PoolKind.MODEL
    ) -> PoolModel:
        """
        Get a PoolModel proxy for a given name / type (as Tryton's Pool().get(...))
        """
        referential = self.models if kind == PoolKind.MODEL else self.wizards
        if name not in referential:
            raise UnknownModelException
        if referential[name] is not None:
            return cast(PoolModel, referential[name])
        result = self._manager.fetch_model(self._key, name, kind)
        model = PoolModel(name, kind, result, self)
        referential[name] = model
        return model

    def fetch_super_information(
        self, model: PoolModel, function_name: str
    ) -> Any:
        return self._manager.fetch_super_information(
            self._key, model.type, model.name, function_name
        )

    def fetch_completions(self, model: PoolModel) -> dict[str, dict[str, Any]]:
        return self._manager.fetch_completions(
            self._key, model.name, model.type
        )


class PoolModel:
    def __init__(
        self, name: ModelName, type: PoolKind, data: dict[str, Any], pool: Pool
    ) -> None:
        super().__init__()
        self._pool: Pool = pool
        self.type: PoolKind = type
        self.name: ModelName = name
        self._dir: set[str] = data["attrs"]
        self.fields: dict[str, Any] = data.get("fields", {})
        self.states: dict[str, Any] = data.get("states", {})
        self._completion_cache: dict[str, dict[str, Any]] | None = None

    def get_super_information(self, function_name: str) -> Any:
        return self._pool.fetch_super_information(self, function_name)

    def has_attribute(self, name: str) -> bool:
        return name in self._dir

    def get_completions(self) -> dict[str, dict[str, Any]]:
        if self._completion_cache is not None:
            return self._completion_cache
        self._completion_cache = self._pool.fetch_completions(self)
        return self._completion_cache

    def generate_completions(self) -> list[CompletionItem]:
        completions = []
        for key, info in self.get_completions().items():
            documentation = ""
            if info["type"] == "field":
                documentation += info["string"]
                if info.get("function", False):
                    documentation += " [Function]"
                if info.get("selection", None):
                    selection = ", ".join(x[0] for x in info["selection"])
                    documentation += "\n\nSelection:\n\n"
                    documentation += selection
                if info.get("domain", None):
                    documentation += "\n\nDomain:\n\n"
                    documentation += info["domain"]
                if info.get("states", None):
                    documentation += "\n\nStates: \n\n"
                    documentation += info["states"]
                completions.append(
                    CompletionItem(
                        label=key,
                        kind=CompletionItemKind.Field,
                        detail=str(info["class_name"]),
                        documentation=documentation,
                    )
                )
            elif info["type"] == "state":
                completions.append(
                    CompletionItem(
                        label=key,
                        kind=CompletionItemKind.Field,
                        detail=str(info["class_name"]),
                    )
                )
            elif info["type"] == "method":
                completions.append(
                    CompletionItem(
                        label=key,
                        kind=CompletionItemKind.Method,
                        documentation=info["documentation"],
                    )
                )
        completions.sort(
            key=lambda x: 0 if x.kind == CompletionItemKind.Field else 10
        )
        return completions


class Module:
    def __init__(
        self, manager: PoolManager, module_name: ModuleName, module_info: dict
    ) -> None:
        super().__init__()
        self._manager: PoolManager = manager
        self._name: ModuleName = module_name
        self._path: Path = self._get_path()
        # keys: filename / classname
        # values: type(model, wizard,...) / module_list
        self._modules_per_class: dict[
            tuple[str, str], tuple[PoolKind, Dependencies]
        ] = self._get_modules_per_class()
        self._module_info: dict = module_info
        self._model_data: dict[str, tuple[Dependencies, etree.Element]] = {}
        self._load_fs_ids()

    def _get_path(self) -> Path:
        try:
            if self._name in ("ir", "res"):
                import_name = f"trytond.{self._name}"
            else:
                import_name = f"trytond.modules.{self._name}"
            imported = importlib.import_module(import_name)
            if imported.__file__ is None:
                raise ImportError(
                    f"Could not locate tryton module {self._name}"
                )
        except ImportError:
            raise ImportError(
                f"Module {self._name} not found as a tryton module"
            )
        return Path(imported.__file__).parent

    def get_directory(self) -> Path:
        return self._path

    def get_module_list_for_import(
        self, filename: str, classname: str
    ) -> tuple[PoolKind, Dependencies] | None:
        """
        Returns how a class defined in a file is registered in Tryton
        """
        return self._modules_per_class.get((filename, classname))

    def get_name(self) -> str:
        return self._name

    def get_model_data(
        self, fs_id: str
    ) -> tuple[Dependencies, etree.Element] | None:
        if self._model_data:
            return self._model_data.get(fs_id, None)
        return None

    def _load_fs_ids(self) -> None:
        for xml_file in self._module_info["xml"]:
            self._model_data.update(self._extract_fs_ids_infos(xml_file))

    def _extract_fs_ids_infos(
        self, filename: str
    ) -> dict[str, tuple[Dependencies, etree.Element]]:
        try:
            with open(self._path / filename) as f:
                parsed = etree.parse(BytesIO(f.read().encode("UTF-8")))
        except etree.XMLSyntaxError:
            return {}
        per_fs_ids = {}
        for data_node in parsed.xpath("/tryton/data"):
            modules = tuple(
                sorted(
                    [self._name]
                    + [
                        x.strip()
                        for x in data_node.attrib.get("depends", "").split(",")
                        if x.strip()
                    ]
                )
            )
            for record in data_node.xpath("record"):
                if "id" not in record.attrib:
                    continue
                per_fs_ids[record.attrib["id"]] = (modules, record)
        return per_fs_ids

    def get_view_info(
        self, filename: str
    ) -> tuple[Dependencies, str, str] | None:
        """
        Returns informations for the requested filename, expecting it to be a
        view.

        Returns:
            - The modules this view depends on
            - The view type (inherit / tree / form...)
            - The model the view is associated to
        """
        for module_list, record in self._model_data.values():
            if record.attrib.get("model", "") != "ir.ui.view":
                continue
            view_file_name = record.xpath("field[@name='name']")
            if not view_file_name:
                continue
            if view_file_name[0].text != filename:
                continue
            model_name = record.xpath("field[@name='model']")
            view_type = record.xpath("field[@name='type']")
            if not model_name or not view_type:
                return None
            return (module_list, view_type[0].text, model_name[0].text)
        return None

    def _get_modules_per_class(
        self,
    ) -> dict[tuple[str, str], tuple[PoolKind, Dependencies]]:
        result: dict[tuple[str, str], tuple[PoolKind, Dependencies]] = {}
        with open(self._path / "__init__.py") as f:
            parsed_init = ast.parse(f.read())

        for register_call in ast.walk(parsed_init):
            if (
                isinstance(register_call, ast.FunctionDef)
                and register_call.name == "register"
            ):
                type_: PoolKind
                for node in register_call.body:
                    if (
                        not isinstance(node, ast.Expr)
                        or not isinstance(node.value, ast.Call)
                        or not isinstance(node.value.func, ast.Attribute)
                        or node.value.func.attr != "register"
                        or not isinstance(node.value.func.value, ast.Name)
                        or node.value.func.value.id != "Pool"
                    ):
                        continue
                    modules = [self._name]
                    cur_imports: list[tuple[str, str]] = []
                    for arg in node.value.args:
                        if not isinstance(arg, ast.Attribute):
                            continue
                        if not isinstance(arg.value, ast.Name):
                            continue
                        cur_imports.append((arg.value.id, arg.attr))
                    for keyword in node.value.keywords:
                        if keyword.arg == "depends" and (
                            isinstance(keyword.value, ast.List)
                        ):
                            modules += [
                                x.value
                                for x in keyword.value.elts
                                if isinstance(x, ast.Constant)
                            ]
                        elif keyword.arg == "type_" and (
                            isinstance(keyword.value, ast.Constant)
                        ):
                            type_ = keyword.value.value
                    result.update(
                        {
                            (file_name, class_name): (
                                type_,
                                tuple(sorted(modules)),
                            )
                            for file_name, class_name in cur_imports
                        }
                    )
        return result
