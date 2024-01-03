import pprint
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

import libcst as cst
import libcst.matchers as m
from libcst.metadata import CodePosition, CodeRange
from lsprotocol.types import Diagnostic as LspDiagnostic
from lsprotocol.types import DiagnosticSeverity, Position, Range
from lxml import etree

if TYPE_CHECKING:
    from .analyzer import PythonAnalyzer, ViewAnalyzer, XMLAnalyzer


class Diagnostic:
    err_code: str
    severity: DiagnosticSeverity

    def __init__(
        self,
        position: CodeRange,
        filepath: Path,
        message_data: dict[str, str] | None = None,
    ) -> None:
        self._position = position
        self._filepath = filepath
        self._init_message_data(message_data or {})

    def __repr__(self) -> str:
        value = f"[{self.severity}]"
        value = self.format_severity()
        value += self.format_error_code()
        value += self.format_location()
        value += self.format_message()
        return value

    def format_severity(self) -> str:
        return f"[{self.severity.name}]"

    def format_error_code(self) -> str:
        return f"[tryton-ls-{self.err_code}]"

    def format_location(self) -> str:
        return f" @ {self._filepath} L{self._position.start.line} "

    def format_message(self) -> str:
        raise NotImplementedError

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        if message_data:
            raise ValueError

    def to_lsp_diagnostic(self) -> LspDiagnostic:
        return LspDiagnostic(
            range=Range(
                start=Position(
                    line=self._position.start.line - 1,
                    character=self._position.start.column,
                ),
                end=Position(
                    line=self._position.end.line - 1,
                    character=self._position.end.column,
                ),
            ),
            severity=self.severity,
            message=f"{self.err_code}: {self.format_message()}",
            code=self.err_code,
            source="TrytonLSP",
        )


class DebugDiagnostic(Diagnostic):
    err_code = "----"
    severity = DiagnosticSeverity.Information

    def __init__(
        self, position: CodeRange, filepath: Path, message: str
    ) -> None:
        super().__init__(position, filepath)
        self._message = message

    def format_message(self) -> str:
        return self._message

    @classmethod
    def init_from_analyzer(
        cls, analyzer: "PythonAnalyzer", node: cst.CSTNode, message: str
    ) -> "DebugDiagnostic":
        return cls(
            analyzer.get_node_position(node),
            filepath=analyzer.get_filepath(),
            message=message,
        )


class ModuleDiagnostic(Diagnostic):
    def __init__(
        self,
        position: CodeRange,
        filepath: Path,
        module_name: str,
        message_data: dict[str, str] | None = None,
    ) -> None:
        super().__init__(position, filepath, message_data)
        self._module_name = module_name


class ModelDiagnostic(ModuleDiagnostic):
    def __init__(
        self,
        position: CodeRange,
        filepath: Path,
        module_name: str,
        model_name: str,
        class_name: str,
        message_data: dict[str, str] | None = None,
    ) -> None:
        super().__init__(position, filepath, module_name, message_data)
        self._model_name = model_name
        self._class_name = class_name

    @classmethod
    def init_from_analyzer(
        cls, analyzer: "PythonAnalyzer", node: cst.CSTNode, **kwargs: Any
    ) -> Optional["ModelDiagnostic"]:
        return cls(
            analyzer.get_node_position(node),
            filepath=analyzer.get_filepath(),
            module_name=analyzer.get_module_name(),
            model_name=analyzer.get_current_model_name(),
            class_name=analyzer.get_current_class_name(),
            message_data=kwargs,
        )


class FunctionDiagnostic(ModelDiagnostic):
    def __init__(
        self,
        position: CodeRange,
        filepath: Path,
        module_name: str,
        model_name: str,
        class_name: str,
        function_name: str,
        message_data: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            position,
            filepath,
            module_name,
            model_name,
            class_name,
            message_data,
        )
        self._function_name = function_name

    @classmethod
    def init_from_analyzer(
        cls, analyzer: "PythonAnalyzer", node: cst.CSTNode, **kwargs: Any
    ) -> Optional["FunctionDiagnostic"]:
        return cls(
            analyzer.get_node_position(node),
            filepath=analyzer.get_filepath(),
            module_name=analyzer.get_module_name(),
            model_name=analyzer.get_current_model_name(),
            class_name=analyzer.get_current_class_name(),
            function_name=analyzer.get_current_function_name(),
            message_data=kwargs,
        )


class MissingRegisterInInit(ModelDiagnostic):
    err_code = "0001"
    severity = DiagnosticSeverity.Warning

    def format_message(self) -> str:
        return (
            f"Class {self._class_name} ('{self._model_name}') "
            "not registered in __init__.py"
        )


class DuplicateName(ModelDiagnostic):
    err_code = "0002"
    severity = DiagnosticSeverity.Warning

    def format_message(self) -> str:
        return f"Class {self._class_name} has multiple __name__ definition "


class ConflictingName(ModelDiagnostic):
    err_code = "0003"
    severity = DiagnosticSeverity.Error

    def format_message(self) -> str:
        return (
            f"Class {self._class_name} has multiple conflicting "
            "__name__ definition "
        )


class SuperInvocationWithParams(FunctionDiagnostic):
    err_code = "1001"
    severity = DiagnosticSeverity.Information

    def format_message(self) -> str:
        return "'super' invocation does not need parameters"


class SuperInvocationMismatchedName(FunctionDiagnostic):
    err_code = "1002"
    severity = DiagnosticSeverity.Error
    expected_name: str

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        self.expected_name = message_data.pop("expected_name")

    def format_message(self) -> str:
        return (
            "'super' call must use the same name "
            + f"(other: '{self.expected_name}')"
        )


class SuperWithoutParent(FunctionDiagnostic):
    err_code = "1003"
    severity = DiagnosticSeverity.Error

    def format_message(self) -> str:
        return "No parent found for super call in parent modules"


class MissingSuperCall(FunctionDiagnostic):
    err_code = "1004"
    severity = DiagnosticSeverity.Error

    def format_message(self) -> str:
        return "Missing super call!"


class UnknownPoolKey(FunctionDiagnostic):
    err_code = "1005"
    severity = DiagnosticSeverity.Error
    possible_values: str

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        self.possible_values = message_data.pop("possible_values")

    def format_message(self) -> str:
        return (
            "Unknown type for Pool().get, possible values are: "
            + f"{self.possible_values}"
        )


class UnknownModel(FunctionDiagnostic):
    err_code = "1006"
    severity = DiagnosticSeverity.Error
    unknown_name: str

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        self.unknown_name = message_data.pop("unknown_name")

    def format_message(self) -> str:
        return f"Could not find '{self.unknown_name}' in the pool"


class UnknownAttribute(FunctionDiagnostic):
    err_code = "1007"
    severity = DiagnosticSeverity.Error
    attr_name: str
    model_name: str

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        self.attr_name = message_data.pop("attr_name")
        self.model_name = message_data.pop("model_name")

    def format_message(self) -> str:
        return f"Unknown attribute '{self.attr_name}' on model '{self.model_name}'"


class ChangeVariableModel(FunctionDiagnostic):
    err_code = "1008"
    severity = DiagnosticSeverity.Warning
    previous_model: str
    new_model: str

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        self.previous_model = message_data.pop("previous_model")
        self.new_model = message_data.pop("new_model")

    def format_message(self) -> str:
        return f"Switching models, from '{self.previous_model}' to '{self.new_model}'"


class XMLDiagnostic(Diagnostic):
    @classmethod
    def init_from_analyzer(
        cls,
        analyzer: Union["XMLAnalyzer", "ViewAnalyzer"],
        node: etree.Element,
        **kwargs: Any,
    ) -> "XMLDiagnostic":
        return cls(
            analyzer.get_node_position(node),
            filepath=analyzer.get_filepath(),
            message_data=kwargs,
        )


class TrytonTagNotFound(XMLDiagnostic):
    err_code = "5000"
    severity = DiagnosticSeverity.Error

    def __init__(
        self,
        filepath: Path,
    ) -> None:
        super().__init__(
            position=CodeRange(
                start=CodePosition(line=1, column=0),
                end=CodePosition(line=1, column=99),
            ),
            filepath=filepath,
        )

    def format_message(self) -> str:
        return "<tryton> tag not found, but file is defined in tryton.cfg"


class TrytonXmlFileUnregistered(XMLDiagnostic):
    err_code = "5001"
    severity = DiagnosticSeverity.Warning

    def __init__(
        self,
        filepath: Path,
    ) -> None:
        super().__init__(
            position=CodeRange(
                start=CodePosition(line=1, column=0),
                end=CodePosition(line=1, column=99),
            ),
            filepath=filepath,
        )

    def format_message(self) -> str:
        return "<tryton> tag found, but file is not defined in tryton.cfg"


class UnexpectedXMLTag(XMLDiagnostic):
    err_code = "5002"
    severity = DiagnosticSeverity.Error
    tag_name: str

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        self.tag_name = message_data.pop("tag_name")

    def format_message(self) -> str:
        return f"Unexpected element '<{self.tag_name}>' here"


class RecordMissingAttribute(XMLDiagnostic):
    err_code = "5003"
    severity = DiagnosticSeverity.Error
    attr_name: str

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        self.attr_name = message_data.pop("attr_name")

    def format_message(self) -> str:
        return f"Missing '{self.attr_name}' attribute"


class RecordUnknownModel(XMLDiagnostic):
    err_code = "5004"
    severity = DiagnosticSeverity.Error
    model_name: str

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        self.model_name = message_data.pop("model_name")

    def format_message(self) -> str:
        return f"Model '{self.model_name}' does not exist in this context"


class RecordUnknownField(XMLDiagnostic):
    err_code = "5005"
    severity = DiagnosticSeverity.Error
    model_name: str
    field_name: str

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        self.model_name = message_data.pop("model_name")
        self.field_name = message_data.pop("field_name")

    def format_message(self) -> str:
        return (
            f"Unknown field '{self.field_name}' on model '{self.model_name}'"
        )


class RecordDuplicateId(XMLDiagnostic):
    err_code = "5006"
    severity = DiagnosticSeverity.Error
    fs_id: str
    other_line: str

    def _init_message_data(self, message_data: dict[str, str]) -> None:
        self.fs_id = message_data.pop("fs_id")
        self.other_line = message_data.pop("other_line")

    def format_message(self) -> str:
        return f"Id {self.fs_id} is already defined line {self.other_line}"


def print_diagnostics(module_name: str, diagnostics: list[Diagnostic]) -> None:
    print(module_name)
    pprint.pprint(diagnostics)


def ignore_error_code(node: cst.CSTNode, ignore_code: str) -> bool:
    # TODO: tox.ini to allow for global deactivations
    return bool(
        m.extractall(
            node,
            m.Comment(
                value=m.MatchIfTrue(
                    lambda value: f"IGNORE-TRYTON-LS-{ignore_code}" in value
                )
            ),
        )
    )
