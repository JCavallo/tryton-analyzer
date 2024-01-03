from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

import libcst as cst
from libcst import CSTNode, ParserSyntaxError
from lxml import etree

if TYPE_CHECKING:
    from .analyzer import Analyzer
    from .pool import Module, PoolManager


class ParsingError(Exception):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path


class ParsingSyntaxError(ParsingError):
    def __repr__(self) -> str:
        return f"Syntax Error parsing contents of {self.path}"


class ModuleNotFoundError(ParsingError):
    def __repr__(self) -> str:
        return f"Could not identify module from path {self.path}"


class ParsedFile:
    def __init__(self, path: Path, data: str | None = None):
        super().__init__()
        self._path: Path = path

        self._module_path: Path | None = self._find_module_path()
        self._module_name: str = (
            self._module_path.stem if self._module_path else ""
        )
        self._module: Module | None = None
        if not data:
            with open(path) as f:
                data = f.read()
        self._raw_data: str = data
        self._raw_lines: list[str] = data.splitlines()
        self._parse(data)

    def _find_module_path(self) -> Path | None:
        names = [self._path.stem]
        for path in self._path.parents:
            names.append(path.stem)
            if (path / "tryton.cfg").is_file():
                return path
        return None

    def _parse(self, data: str) -> None:
        raise ParsingSyntaxError(self._path)

    def get_module_name(self) -> str:
        return self._module_name

    def get_module_path(self) -> Path:
        if self._module_path:
            return self._module_path
        raise ModuleNotFoundError(self._path)

    def get_filename(self) -> str:
        return self._path.stem

    def get_path(self) -> Path:
        return self._path

    def set_module(self, module: Module | None) -> None:
        self._module = module

    def get_module(self) -> Module | None:
        return self._module

    def get_analyzer(self, pool_manager: PoolManager) -> Analyzer:
        raise NotImplementedError

    def get_parsed(self) -> Any:
        raise NotImplementedError

    def get_raw_lines(self) -> list[str]:
        return self._raw_lines


class ParsedXMLFile(ParsedFile):
    _parsed: etree.iterparse

    def __init__(self, path: Path, data: str | None = None):
        super().__init__(path, data)

    def _parse(self, data: str) -> None:
        # We want a lazy iteration, so we rely on "get_parsed" to get an
        # iterator rather than parsing all at once
        pass

    def get_parsed(self) -> etree.iterparse:
        return etree.iterparse(
            BytesIO(self._raw_data.encode("UTF-8")),
            events=["start", "comment", "end"],
        )

    def get_analyzer(self, pool_manager: PoolManager) -> Analyzer:
        from .analyzer import XMLAnalyzer

        return XMLAnalyzer(self, pool_manager)


class ParsedViewFile(ParsedFile):
    _parsed: etree.iterparse

    def __init__(self, path: Path, data: str | None = None):
        super().__init__(path, data)

    def _parse(self, data: str) -> None:
        # We want a lazy iteration, so we rely on "get_parsed" to get an
        # iterator rather than parsing all at once
        pass

    def get_parsed(self) -> etree.iterparse:
        return etree.iterparse(
            BytesIO(self._raw_data.encode("UTF-8")),
            events=["start", "comment", "end"],
        )

    def get_analyzer(self, pool_manager: PoolManager) -> Analyzer:
        from .analyzer import ViewAnalyzer

        return ViewAnalyzer(self, pool_manager)


class ParsedPythonFile(ParsedFile):
    _parsed: CSTNode

    def __init__(self, path: Path, data: str | None = None):
        super().__init__(path, data)
        self._import_path: str | None = self._find_import_path()

    def _find_import_path(self) -> str | None:
        names = [self._path.stem]
        for path in self._path.parents:
            names.append(path.stem)
            if (path / "tryton.cfg").is_file():
                return "trytond.modules." + ".".join(reversed(names))
        return None

    def get_import_path(self) -> str:
        if self._import_path:
            return self._import_path
        raise ModuleNotFoundError(self._path)

    def _parse(self, data: str) -> None:
        try:
            self._parsed = cst.parse_module(data)
        except ParserSyntaxError:
            raise ParsingSyntaxError(self._path)

    def get_parsed(self) -> CSTNode:
        return self._parsed

    def get_analyzer(self, pool_manager: PoolManager) -> Analyzer:
        from .analyzer import PythonAnalyzer

        return PythonAnalyzer(self, pool_manager)
