from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

import libcst as cst
import libcst.matchers as m
from libcst.metadata import CodePosition, CodeRange, PositionProvider
from lxml import etree

from .parsing import (
    ParsedFile,
    ParsedPythonFile,
    ParsedViewFile,
    ParsedXMLFile,
)
from .tools import (
    ChangeVariableModel,
    ConflictingName,
    DebugDiagnostic,
    Diagnostic,
    DuplicateName,
    MissingRegisterInInit,
    MissingSuperCall,
    RecordDuplicateId,
    RecordMissingAttribute,
    RecordUnknownField,
    RecordUnknownModel,
    SuperInvocationMismatchedName,
    SuperInvocationWithParams,
    SuperWithoutParent,
    TrytonTagNotFound,
    TrytonXmlFileUnregistered,
    UnexpectedXMLTag,
    UnknownAttribute,
    UnknownModel,
    UnknownPoolKey,
    ignore_error_code,
)

if TYPE_CHECKING:
    from .pool import Pool, PoolKind, PoolManager, PoolModel


class CompletionTargetFoundError(Exception):
    pass


class Analyzer:
    _parsed: Any

    def __init__(
        self, parsed: ParsedFile, pool_manager: "PoolManager"
    ) -> None:
        super().__init__()
        self._parsed = parsed.get_parsed()
        self._filename = parsed.get_filename()
        self._filepath = parsed.get_path()
        self._module = parsed.get_module()
        self._raw_lines = parsed.get_raw_lines()
        self._module_path = parsed.get_module_path()
        self._pool_manager = pool_manager
        self._diagnostics: list[Diagnostic] = []

    def add_diagnostic(self, diagnostic: Diagnostic | None) -> None:
        if diagnostic and not self.ignored(
            diagnostic.err_code, diagnostic._position.start.line
        ):
            self._diagnostics.append(diagnostic)

    def get_filepath(self) -> Path:
        return self._filepath

    def get_module_name(self) -> str:
        if not self._module:
            return ""
        return self._module.get_name()

    def analyze(
        self, ranges: list[CodeRange] | None = None
    ) -> list[Diagnostic]:
        raise NotImplementedError

    def ignored(self, err_code: str, line_number: int) -> bool:
        # TODO: Improve resiliency
        return (
            f"IGNORE-TRYTON-LS-{err_code}" in self._raw_lines[line_number - 1]
        )


class TrytonMetadata:
    def __init__(
        self,
        pool: "Pool",
        model: Optional["PoolModel"] = None,
        model_name: str = "",
        kind: str | None = None,
        class_name: str = "",
    ) -> None:
        super().__init__()
        self._model = model
        self._model_name = model_name
        self._kind = kind
        self._pool = pool
        self._class_name = class_name

    def __repr__(self) -> str:
        return (
            f"{self._model_name} ({self._kind}): {self._model} ({self._pool})"
        )


class PythonAnalyzer(Analyzer, cst.CSTVisitor):
    METADATA_DEPENDENCIES = (PositionProvider,)
    _parsed: cst.CSTNode

    def __init__(
        self, parsed: ParsedPythonFile, pool_manager: "PoolManager"
    ) -> None:
        super().__init__(parsed, pool_manager)
        self._import_path = parsed.get_import_path()
        self._metadata: dict[cst.ClassDef, TrytonMetadata | None] = {}
        self._positions: dict[CodeRange, cst.CSTNode] = {}
        self._cur_class: cst.ClassDef | None = None
        self._cur_function: cst.FunctionDef | None = None
        self._class_for_function: dict[
            cst.FunctionDef, cst.ClassDef | None
        ] = {}
        self._class_metadata: TrytonMetadata | None = None

        # Variables which are instances of Pool
        # Typical use case will be "pool = Pool()"
        self._function_pool_vars: set[str] = set()

        # Variables identified as a given model
        self._node_mappings: dict[cst.CSTNode | str, "PoolModel"] = {}

        # List of variables identified as a given model
        self._node_list_mappings: dict[cst.CSTNode | str, "PoolModel"] = {}

        # Ranges to limit the parsing to, used to interrupt search when looking
        # for completions
        self._ranges: list[CodeRange] = []

    def _track_position(self, node: cst.CSTNode) -> None:
        self._positions[self.get_metadata(PositionProvider, node)] = node

    def _must_analyze(self, node: cst.CSTNode) -> bool:
        """
        Used when completing to only analyze the class / function of the
        completion point
        """
        if not self._ranges:
            return True
        metadata = self.get_metadata(PositionProvider, node)

        def check_range(range: CodeRange) -> bool:
            if range.end.line < metadata.start.line:
                return False
            if (
                range.end.line == metadata.start.line
                and range.end.column < metadata.start.column
            ):
                return False
            if range.start.line > metadata.end.line:
                return False
            if (
                range.start.line == metadata.end.line
                and range.start.column > metadata.end.column
            ):
                return False
            return True

        return any(check_range(x) for x in self._ranges)

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if not self._must_analyze(node):
            return False
        self._track_position(node)
        self._cur_class = node
        # The __name__ = '...' line
        model_node = self._model_name(node)
        if model_node and self._module:
            model_name = model_node.value[1:-1]
            class_name = node.name.value
            import_info = self._module.get_module_list_for_import(
                self._filename, class_name
            )
            if not import_info:
                # We could not find the class in the __init__ file, meaning it
                # is not registered in the pool, even though it has a __name__
                self._class_metadata = TrytonMetadata(
                    self._pool_manager.get_pool((self._module.get_name(),)),
                    model=None,
                    model_name=model_name,
                    class_name=class_name,
                )
                self.add_diagnostic(
                    MissingRegisterInInit.init_from_analyzer(self, model_node)
                )
            else:
                kind, module_list = import_info
                pool = self._pool_manager.get_pool(tuple(module_list))
                try:
                    model = pool.get(model_name, kind)
                except KeyError:
                    model = None
                # Use this model while parsing the class
                self._class_metadata = TrytonMetadata(
                    pool,
                    model=model,
                    model_name=model_name,
                    class_name=class_name,
                    kind=kind,
                )
                if model is None:
                    # Model was not found in the pool
                    self.add_diagnostic(
                        UnknownModel.init_from_analyzer(
                            self, model_node, unknown_name=model_name
                        )
                    )
        elif self._module:
            self._class_metadata = TrytonMetadata(
                self._pool_manager.get_pool((self._module.get_name(),))
            )
        else:
            self._class_metadata = None
        self._metadata[node] = self._class_metadata
        return True

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        # Reset the current model
        self._cur_class = None
        self._class_metadata = None

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        if not self._must_analyze(node):
            return False
        self._track_position(node)
        self._cur_function = node
        self._class_for_function[node] = self._cur_class
        # Reset known names
        self._node_list_mappings.clear()
        self._node_mappings.clear()
        self._function_pool_vars.clear()
        if self._class_metadata and self._class_metadata._model:
            self._node_mappings = {
                "self": self._class_metadata._model,
                "cls": self._class_metadata._model,
            }
        self.analyze_function(node)
        return True

    def get_import_path(self) -> str:
        """
        Get the qualified import path for the current class
        """
        if not self._class_metadata:
            return self._import_path
        return f"'{self._import_path}.{self.get_current_class_name()}'"

    def get_function_name(self) -> str:
        if not self._cur_function:
            return ""
        return self._cur_function.name.value

    def get_current_metadata(self) -> TrytonMetadata | None:
        return self._class_metadata

    def get_current_class_node(self) -> cst.ClassDef | None:
        return self._cur_class

    def get_current_function_node(self) -> cst.FunctionDef | None:
        return self._cur_function

    def get_pool(self) -> Optional["Pool"]:
        """
        Return a pool containing the current class, or the pool of the current
        module if we are not currently inside a class
        """
        if not self._class_metadata:
            if self._module:
                return self._pool_manager.get_pool((self._module.get_name(),))
            return None
        return self._class_metadata._pool

    def analyze_function(self, node: cst.FunctionDef) -> None:
        """
        Check function definition for errors
        """
        self._check_parameters(node)
        self._check_super_call(node)
        self._check_depends(node)

    def _check_parameters(self, node: cst.FunctionDef) -> None:
        """
        Checks the function's parameters (and try to assign models when
        possible).
        """
        pool = self.get_pool()
        if not pool:
            return
        for match in m.extractall(
            node.params,
            # Matches arguments annotated with:
            #   - Record: current model
            #   - Records: list of current model
            m.Param(
                name=m.SaveMatchedNode(m.Name(), "param_name"),
                annotation=m.Annotation(
                    annotation=m.SaveMatchedNode(
                        m.Name(value="Record") | m.Name(value="Records"),
                        "annotation_name",
                    )
                ),
            )
            # Matches arguments annotated with:
            #   - Record["some_model"]: instance of Pool().get("some_model")
            #   - Records["some_model"]: list of instances of
            #       Pool().get("some_model")
            | m.Param(
                name=m.SaveMatchedNode(m.Name(), "param_name"),
                annotation=m.Annotation(
                    annotation=m.Subscript(
                        value=m.SaveMatchedNode(
                            m.Name(value="Record") | m.Name(value="Records"),
                            "annotation_name",
                        ),
                        slice=[
                            m.SubscriptElement(
                                slice=m.Index(
                                    value=m.SaveMatchedNode(
                                        m.SimpleString(), "model_name"
                                    )
                                )
                            )
                        ],
                    )
                ),
            ),
        ):
            model: PoolModel | None = None
            if "model_name" in match:
                model_name: cst.Name = cast(cst.Name, match["model_name"])
                try:
                    model = pool.get(
                        model_name.value[1:-1],
                        cast("PoolKind", "model"),
                    )
                except KeyError:
                    self.add_diagnostic(
                        UnknownModel.init_from_analyzer(
                            self,
                            model_name,
                            unknown_name=model_name.value[1:-1],
                        )
                    )
            elif self._class_metadata is not None:
                model = self._class_metadata._model
            else:
                model = None
            if model is None:
                continue
            annotation_name: cst.Name = cast(
                cst.Name, match["annotation_name"]
            )
            param_name: cst.Name = cast(cst.Name, match["param_name"])
            if annotation_name.value == "Record":
                self._node_mappings[param_name.value] = model
            else:
                self._node_list_mappings[param_name.value] = model

    def _check_depends(self, node: cst.FunctionDef) -> None:
        """
        Check the depends (as in @fields.depends) of a function
        """
        if not self._class_metadata:
            return
        model = self._class_metadata._model
        if not model:
            return
        pool = self._class_metadata._pool

        for decorator in node.decorators:
            # Only work on the @fields.depends decorator
            if not m.matches(
                decorator,
                m.Decorator(
                    decorator=m.Call(
                        func=m.Attribute(
                            value=m.Name(value="fields"),
                            attr=m.Name(value="depends"),
                        )
                    )
                ),
            ):
                continue
            # Iterate on all parameters of the decorator
            for arg in cast(cst.Call, decorator.decorator).args:
                if m.matches(arg, m.Arg(value=m.SimpleString(), keyword=None)):
                    # Basic case: we expect the name of a field, or maybe
                    # _parent_xxxx.yyyy fields
                    cur_model = model
                    field_names = (
                        cast(cst.SimpleString, arg.value)
                        .value[1:-1]
                        .split(".")
                    )
                    for field_name in field_names[:-1]:
                        # Parse everything before the dot
                        if not field_name.startswith("_parent_"):
                            # TODO: dedicated diagnostic
                            self.add_diagnostic(
                                UnknownAttribute.init_from_analyzer(
                                    self,
                                    arg.value,
                                    model_name=cur_model.name,
                                    attr_name=field_name,
                                )
                            )
                            break
                        # Remove the "_parent_" part
                        field_name = field_name[8:]
                        if field_name not in cur_model.fields:
                            self.add_diagnostic(
                                UnknownAttribute.init_from_analyzer(
                                    self,
                                    arg.value,
                                    model_name=cur_model.name,
                                    attr_name=field_name,
                                )
                            )
                            break
                        field = cur_model.fields[field_name]
                        if field["type"] != "many2one":
                            # TODO: dedicated diagnostic
                            self.add_diagnostic(
                                UnknownAttribute.init_from_analyzer(
                                    self,
                                    arg.value,
                                    model_name=cur_model.name,
                                    attr_name=field_name,
                                )
                            )
                            break
                        try:
                            # Change the current model, and go to the next
                            # "dot" (or the final part) of the depends
                            cur_model = pool.get(
                                field["relation"], cast("PoolKind", "model")
                            )
                        except KeyError:
                            self.add_diagnostic(
                                UnknownModel.init_from_analyzer(
                                    self,
                                    arg.value,
                                    unknown_name=field["relation"],
                                )
                            )
                            break
                    else:
                        # Actual field name we need to check
                        field_name = field_names[-1]
                        if field_name not in cur_model.fields:
                            self.add_diagnostic(
                                UnknownAttribute.init_from_analyzer(
                                    self,
                                    arg.value,
                                    model_name=cur_model.name,
                                    attr_name=field_name,
                                )
                            )
                elif m.matches(arg, m.Arg(value=m.List(), keyword=m.Name())):
                    # Case of @fields.depends(methods=[...])
                    if cast(cst.Name, arg.keyword).value != "methods":
                        # TODO: dedicated diagnostic
                        self.add_diagnostic(
                            UnknownAttribute.init_from_analyzer(
                                self,
                                arg.value,
                                model_name=cur_model.name,
                                attr_name=field_name,
                            )
                        )
                        continue
                    for method in cast(cst.List, arg.value).elements:
                        if not m.matches(
                            method, m.Element(value=m.SimpleString())
                        ):
                            continue
                        method_name = cast(
                            cst.SimpleString, method.value
                        ).value[1:-1]
                        # For method, we just check that it exists on the model
                        if not model.has_attribute(method_name):
                            self.add_diagnostic(
                                UnknownAttribute.init_from_analyzer(
                                    self,
                                    arg.value,
                                    model_name=model.name,
                                    attr_name=method_name,
                                )
                            )

    def _check_super_call(self, node: cst.FunctionDef) -> None:
        """
        We want to make sure that an overriden method actually calls super (and
        a few other things)"""
        # Find all calls to super in the function's body
        super_call = m.extractall(
            node,
            m.Call(
                func=m.Attribute(
                    value=m.SaveMatchedNode(
                        m.Call(func=m.Name(value="super")), "super_invocation"
                    ),
                    attr=m.SaveMatchedNode(m.Name(), "super_name"),
                )
            ),
        )
        for cur_call in super_call:
            super_invocation: cst.Call = cast(
                cst.Call, cur_call["super_invocation"]
            )
            super_name: cst.Name = cast(cst.Name, cur_call["super_name"])
            if super_invocation.args:
                # Detect if super(Move, self) is used rather than plain super()
                # TODO: Add a way to disable this, it's mostly style
                self.add_diagnostic(
                    SuperInvocationWithParams.init_from_analyzer(
                        self, super_invocation
                    )
                )
            if super_name.value != node.name.value:
                # Detects if we call super().function_b from inside function_a
                self.add_diagnostic(
                    SuperInvocationMismatchedName.init_from_analyzer(
                        self,
                        super_name,
                        expected_name=node.name.value,
                    )
                )

        if not self._class_metadata:
            return
        model = self._class_metadata._model
        if not model:
            return

        # We want to know if we missed a super call (or if a super call was
        # made where no super exists), so we must interact with Tryton through
        # the companion to get some informations
        has_super = bool(super_call)
        found_base, found_super = False, False
        function_name = self.get_current_function_name()
        # We iterate over the mro, returning only classes that define /
        # override the current function
        #   - klass_str contains the qualified name of the class for matching
        #       (trytond.modules.account_invoice.invoice.Invoice)
        #   - details contains the filepath / line number of the parent
        #       function declaration
        for klass_str, details in model.get_super_information(function_name):
            if self.get_import_path() in klass_str:
                found_base = True
            elif found_base and not has_super and details:
                if details == "no_code":
                    # TODO: properties do not have code ?
                    break
                parent_file_name, first_line_no = details
                parent_parsed = self._pool_manager.get_parsed(
                    Path(parent_file_name)
                )
                if parent_parsed is None:
                    # TODO: Error
                    break
                # We use a specialized parser to quicly find the current parent
                # function for parsing
                wrapper = cst.MetadataWrapper(
                    parent_parsed.get_parsed(), unsafe_skip_copy=True
                )
                finder = FunctionFinder(lineno=first_line_no)
                wrapper.visit(finder)
                if finder._match and not ignore_error_code(
                    finder._match, MissingSuperCall.err_code
                ):
                    # The current function has a parent, but super is not
                    # called, that's an error
                    self.add_diagnostic(
                        MissingSuperCall.init_from_analyzer(self, node.name)
                    )
                break
            elif (
                found_base
                and has_super
                and not found_super
                and details is not None
            ):
                found_super = True
        if has_super and not found_super:
            # The current function has a super call, but we could not
            # find a parent definition => Error
            self.add_diagnostic(
                SuperWithoutParent.init_from_analyzer(self, node.name)
            )

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        # Reset everything
        self._cur_function = None
        self._node_mappings.clear()
        self._node_list_mappings.clear()
        self._function_pool_vars.clear()

    def _model_name(self, node: cst.ClassDef) -> cst.SimpleString | None:
        """
        Search for the __name__ of the class
        """
        extract_data = m.extractall(
            node,
            m.Assign(
                targets=[m.AssignTarget(target=m.Name(value="__name__"))],
                value=m.SaveMatchedNode(m.SimpleString(), "__name__"),
            ),
        )
        if not extract_data:
            return None
        match, *extra_matches = extract_data
        if "__name__" in match:
            name_definition: cst.SimpleString = cast(
                cst.SimpleString, match["__name__"]
            )
            for extra in extra_matches:
                # More than one '__name__ = "..."' in the body of the class
                if "__name__" in extra:
                    extra_match: cst.SimpleString = cast(
                        cst.SimpleString, extra["__name__"]
                    )
                    if name_definition.value == extra_match.value:
                        self.add_diagnostic(
                            DuplicateName(
                                self.get_node_position(extra_match),
                                self._filepath,
                                self.get_module_name(),
                                name_definition.value[1:-1],
                                node.name.value,
                            )
                        )
                    else:
                        self.add_diagnostic(
                            ConflictingName(
                                self.get_node_position(extra_match),
                                self._filepath,
                                self.get_module_name(),
                                name_definition.value[1:-1],
                                node.name.value,
                            )
                        )

            return name_definition
        return None

    def leave_Attribute(self, node: cst.Attribute) -> None:
        """
        When we exit a x.y, and we knew the model of x, we can set the model of
        x.y if available.

        We can also know if y exists on x, and raise an error if we cannot find
        it
        """
        model = self._get_type(node.value)
        if model is None:
            return
        attr = node.attr.value
        if not model.has_attribute(attr):
            # The error that is useful enough by itself to justify all this :D
            self.add_diagnostic(
                UnknownAttribute.init_from_analyzer(
                    self,
                    node.attr,
                    model_name=model.name,
                    attr_name=attr,
                )
            )
        pool = self.get_pool()
        if not pool:
            return
        if model.type == "model":
            if (
                attr not in model.fields
                or "relation" not in model.fields[attr]
            ):
                # We can only "go further" if the "y" is a field with a
                # relation
                return
            field = model.fields[attr]
            target = pool.get(field["relation"])
            if field["type"] == "many2one":
                self._node_mappings[node] = target
            else:
                self._node_list_mappings[node] = target
        elif model.type == "wizard":
            if attr not in model.states:
                return
            self._node_mappings[node] = pool.get(
                model.states[attr]["relation"]
            )

    def visit_Call(self, node: cst.Call) -> None:
        """
        Identify model of function calls. For now we only want to do:
            - Account.search(...)
            - Account.browse(...)
            - Account(...)
        """
        value_type = self._get_type(node.func)
        if value_type is not None:
            # instantiation
            self._node_mappings[node] = value_type
            return
        extracted = m.extract(
            node,
            m.Call(
                func=m.Attribute(
                    value=m.SaveMatchedNode(
                        m.MatchIfTrue(lambda x: x in self._node_mappings),
                        "match",
                    )
                    | m.Name(
                        value=m.SaveMatchedNode(
                            m.MatchIfTrue(lambda x: x in self._node_mappings),
                            "match",
                        )
                    ),
                    attr=m.Name(),
                )
            ),
        )
        if extracted:
            value_type = extracted["match"]
            func_name = cast(cst.Attribute, node.func).attr.value
            if func_name in ("search", "browse"):
                self._node_list_mappings[node] = self._node_mappings[
                    value_type
                ]

    def visit_For(self, node: cst.For) -> None:
        """
        Assign the type of the expression we iterate on to the variable if
        unambiguous
        """
        node.iter.visit(self)
        base_type = self._get_list_type(node.iter)
        if base_type is None:
            return
        self._node_mappings[cast(cst.Name, node.target).value] = base_type

    def visit_ListComp(self, node: cst.ListComp) -> None:
        node.for_in.visit(self)

    def leave_ListComp(self, node: cst.ListComp) -> None:
        self._analyze_comp(node)

    def visit_SetComp(self, node: cst.SetComp) -> None:
        self._analyze_comp(node)

    def leave_CompFor(self, node: cst.CompFor) -> None:
        if not m.matches(node.target, m.Name()):
            return
        base_type = self._get_list_type(node.iter)
        if base_type is None:
            return
        self._node_mappings[cast(cst.Name, node.target).value] = base_type

    def _analyze_comp(self, node: cst.BaseSimpleComp) -> None:
        base_type = self._get_type(node.elt)
        if base_type is None:
            return
        self._node_list_mappings[node] = base_type

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        pool = self.get_pool()
        if not pool:
            return
        match = m.extract(
            node.annotation,
            m.Annotation(
                annotation=m.SaveMatchedNode(
                    m.Name(value="Record") | m.Name(value="Records"),
                    "annotation_name",
                )
            )
            | m.Annotation(
                annotation=m.Subscript(
                    value=m.SaveMatchedNode(
                        m.Name(value="Record") | m.Name(value="Records"),
                        "annotation_name",
                    ),
                    slice=[
                        m.SubscriptElement(
                            slice=m.Index(
                                value=m.SaveMatchedNode(
                                    m.SimpleString(), "model_name"
                                )
                            )
                        )
                    ],
                )
            ),
        )
        if not match:
            return
        model: PoolModel | None = None
        if "model_name" in match:
            model_name: cst.Name = cast(cst.Name, match["model_name"])
            try:
                model = pool.get(
                    model_name.value[1:-1],
                    cast("PoolKind", "model"),
                )
            except KeyError:
                self.add_diagnostic(
                    UnknownModel.init_from_analyzer(
                        self,
                        model_name,
                        unknown_name=model_name.value[1:-1],
                    )
                )
        elif self._class_metadata is not None:
            model = self._class_metadata._model
        else:
            model = None
        if model is None:
            return
        annotation_name: cst.Name = cast(cst.Name, match["annotation_name"])
        if annotation_name.value == "Record":
            self._node_mappings[node] = model
        else:
            self._node_list_mappings[node] = model

    def leave_AnnAssign(self, node: cst.AnnAssign) -> None:
        if not m.matches(node.target, m.Name()):
            return
        target = cast(cst.Name, node.target).value

        value_type = self._get_type(cast(cst.CSTNode, node.value))
        if (
            value_type
            and node in self._node_mappings
            and value_type != self._node_mappings[node]
        ):
            self.add_diagnostic(
                ChangeVariableModel.init_from_analyzer(
                    self,
                    node.target,
                    previous_model=self._node_mappings[node].name,
                    new_model=value_type.name,
                )
            )
        if value_type or node in self._node_mappings:
            self._node_mappings[target] = (
                value_type or self._node_mappings[node]
            )
            return

        value_type = self._get_list_type(cast(cst.CSTNode, node.value))
        if (
            value_type
            and node in self._node_list_mappings
            and value_type != self._node_list_mappings[node]
        ):
            self.add_diagnostic(
                ChangeVariableModel.init_from_analyzer(
                    self,
                    node.target,
                    previous_model=self._node_mappings[node].name,
                    new_model=value_type.name,
                )
            )
        if value_type or node in self._node_list_mappings:
            self._node_list_mappings[target] = (
                value_type or self._node_list_mappings[node]
            )
            return

        self._node_mappings.pop(target, None)
        self._node_list_mappings.pop(target, None)

    def leave_Assign(self, node: cst.Assign) -> None:
        """
        Set type to a variable based on the expression we assign to it
        """
        # If the node is a "Pool().get", nothing to do
        if self._check_is_pool(node):
            return

        mono, multi = None, []
        if m.matches(
            node.targets[0],
            m.AssignTarget(
                target=m.Tuple(
                    elements=[
                        m.AtLeastN(n=1, matcher=m.Element(value=m.Name()))
                    ]
                )
            ),
        ):
            target: cst.Tuple = cast(cst.Tuple, node.targets[0].target)
            multi = [
                cast(cst.Name, name.value).value
                for name in target.elements
                if m.matches(name, m.Element(value=m.Name()))
            ]
        elif m.matches(node.targets[0].target, m.Name()):
            mono = cast(cst.Name, node.targets[0].target).value
        else:
            return

        # If the assigned value is a single record
        value_type = self._get_type(node.value)
        if value_type is not None:
            if mono:
                # If we assign to a single value, we can set its model
                self._node_mappings[mono] = value_type
            for name in multi:
                # If me assign to mulitple records, we clear them
                # TODO: Error?
                self._node_mappings.pop(name, None)
                self._node_list_mappings.pop(name, None)
            return

        # If the assigned value is a list of records
        value_type = self._get_list_type(node.value)
        if value_type is not None:
            if mono:
                # If we assign to a single value, it is a list of records
                self._node_list_mappings[mono] = value_type
            elif multi:
                for name in multi:
                    self._node_mappings[name] = value_type
            else:
                for name in multi + ([mono] if mono else []):
                    self._node_mappings.pop(name, None)
                    self._node_list_mappings.pop(name, None)
            return

        # If we do not know the type, reset everything
        for name in multi + ([mono] if mono else []):
            self._node_mappings.pop(name, None)
            self._node_list_mappings.pop(name, None)

    def leave_Subscript(self, node: cst.Subscript) -> None:
        """
        Assign types when using my_list[X] or my_list[X:Y]
        """
        if m.matches(
            node,
            m.Subscript(slice=[m.SubscriptElement(slice=m.Index())])
            | m.Subscript(slice=[m.SubscriptElement(slice=m.Slice())]),
        ):
            value_type = self._get_list_type(node.value)
            if value_type is not None:
                self._node_mappings[node] = value_type
                return

    def visit_Lambda(self, node: cst.Lambda) -> bool:
        # Avoid errors inside lambda calls where the parameters shadow another
        # variable
        return False

    def _get_type(self, node: cst.CSTNode) -> Any:
        """
        We know the type of a node through the node mappings
        """
        if isinstance(node, cst.Name) and node.value in self._node_mappings:
            return self._node_mappings[node.value]
        if node in self._node_mappings:
            return self._node_mappings[node]
        model = self._handle_pool_get(node)
        if model is not None:
            return model

    def _get_list_type(self, node: cst.CSTNode) -> Any:
        if (
            isinstance(node, cst.Name)
            and node.value in self._node_list_mappings
        ):
            return self._node_list_mappings[node.value]
        if node in self._node_list_mappings:
            return self._node_list_mappings[node]

    def _handle_pool_get(self, node: cst.CSTNode) -> Any:
        pool = self.get_pool()
        if not pool:
            return None
        # Check if the node matches "Pool().get(...)" to identify the record
        # type
        extracted = m.extract(
            node,
            m.Call(
                func=m.Attribute(
                    value=m.OneOf(
                        m.Call(func=m.Name(value="Pool")),
                        m.Name(
                            value=m.MatchIfTrue(
                                lambda value: value in self._function_pool_vars
                            )
                        ),
                    ),
                    attr=m.Name(value="get"),
                ),
                args=[
                    m.Arg(
                        value=m.SaveMatchedNode(m.SimpleString(), "model_name")
                    ),
                    m.AtMostN(
                        n=1,
                        matcher=m.Arg(
                            value=m.SaveMatchedNode(m.SimpleString(), "kind")
                        ),
                    ),
                ],
            ),
        )
        if extracted is not None and "model_name" in extracted:
            model_name: cst.SimpleString = cast(
                cst.SimpleString, extracted["model_name"]
            )
            extracted_kind: cst.SimpleString | None = None
            kind = "model"
            if "kind" in extracted:
                extracted_kind = cast(cst.SimpleString, extracted["kind"])
                kind = extracted_kind.value[1:-1]
                if kind not in pool.supported_keys:
                    # TODO: Remove?
                    self.add_diagnostic(
                        UnknownPoolKey.init_from_analyzer(
                            self,
                            extracted_kind,
                            possible_values=", ".join(pool.supported_keys),
                        )
                    )
            try:
                self._node_mappings[node] = pool.get(
                    model_name.value[1:-1], cast("PoolKind", kind)
                )
                return self._node_mappings[node]
            except KeyError:
                self.add_diagnostic(
                    UnknownModel.init_from_analyzer(
                        self,
                        model_name,
                        unknown_name=model_name.value[1:-1],
                    )
                )

    def _check_is_pool(self, node: cst.Assign) -> bool:
        is_pool = m.extract(
            node,
            m.Assign(
                targets=[
                    m.AssignTarget(
                        target=m.SaveMatchedNode(m.Name(), "var_name")
                    )
                ],
                value=m.Call(func=m.Name(value="Pool")),
            ),
        )
        if is_pool is not None and "var_name" in is_pool:
            var_name: cst.Name = cast(cst.Name, is_pool["var_name"])
            self._function_pool_vars.add(var_name.value)
            return True
        return False

    def add_debug_diagnostic(self, node: cst.CSTNode, message: str) -> None:
        self._diagnostics.append(
            DebugDiagnostic.init_from_analyzer(self, node, message)
        )

    def get_current_model_name(self) -> str:
        return self._class_metadata._model_name if self._class_metadata else ""

    def get_current_class_name(self) -> str:
        return self._class_metadata._class_name if self._class_metadata else ""

    def get_node_position(self, node: cst.CSTNode) -> CodeRange:
        return self.get_metadata(PositionProvider, node)

    def get_current_function_name(self) -> str:
        return self._cur_function.name.value if self._cur_function else ""

    def analyze(
        self, ranges: list[CodeRange] | None = None
    ) -> list[Diagnostic]:
        self._diagnostics = []
        self._ranges = ranges or []
        wrapper = cst.MetadataWrapper(
            cast(cst.Module, self._parsed), unsafe_skip_copy=True
        )
        wrapper.visit(self)
        return self._diagnostics

    def ignored(self, err_code: str, line_number: int) -> bool:
        # Allow to set ignore commentaries above the bad line
        return super().ignored(err_code, line_number) or (
            line_number > 1
            and self._raw_lines[line_number - 2].lstrip().startswith("#")
            and f"IGNORE-TRYTON-LS-{err_code}"
            in self._raw_lines[line_number - 2]
        )


class PythonCompletioner(PythonAnalyzer):
    def __init__(
        self,
        parsed: ParsedPythonFile,
        pool_manager: "PoolManager",
        line: int,
        column: int,
    ) -> None:
        super().__init__(parsed, pool_manager)
        self._line = line
        self._column = column
        self._target_model: "PoolModel" | None = None
        self._target_node: cst.CSTNode | None = None

    def _must_analyze(self, node: cst.CSTNode) -> bool:
        # Only analyze the class / function containing the position to complete
        metadata = self.get_metadata(PositionProvider, node)
        result = metadata.start.line <= self._line <= metadata.end.line
        return result

    def visit_Attribute(self, node: cst.Attribute) -> None:
        metadata = self.get_metadata(PositionProvider, node)
        if metadata.start.line == self._line == metadata.end.line and (
            metadata.start.column <= self._column <= metadata.end.column
        ):
            self._target_node = node
        super().visit_Attribute(node)

    def leave_Attribute(self, node: cst.Attribute) -> None:
        super().leave_Attribute(node)
        if self._target_node != node:
            return
        # When we leave the target of the completion, we set the target model
        # that we identified (if any), then raise the
        # CompletionTargetFoundError to stop the parser
        self._target_model = self._get_type(node.value)
        raise CompletionTargetFoundError


class FunctionFinder(cst.CSTVisitor):
    """
    Very basic Visitor class to find a node by its position
    """

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, lineno: int) -> None:
        super().__init__()
        self._lineno = lineno
        self._match: cst.FunctionDef | None = None

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        position = self.get_metadata(PositionProvider, node)
        return position.start.line <= self._lineno <= position.end.line

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        position = self.get_metadata(PositionProvider, node)
        if position.start.line <= self._lineno <= position.end.line:
            self._match = node
        return False


class XMLAnalyzer(Analyzer):
    def __init__(
        self, parsed: ParsedXMLFile, pool_manager: "PoolManager"
    ) -> None:
        super().__init__(parsed, pool_manager)
        self._ranges: list[CodeRange] = []
        self._fs_ids: dict[str, int] = {}
        self._found_tryton_tag = False
        self._tag_stack: list[etree.Element] = []
        self._current_pool: Optional["Pool"] = None
        self._current_model: Any = None
        self._cur_prefix: int = 0

    def analyze(
        self, ranges: list[CodeRange] | None = None
    ) -> list[Diagnostic]:
        self._diagnostics = []
        self._ranges = ranges or []

        is_tryton_xml = self.check_is_tryton_xml_file()

        try:
            for action, element in self._parsed:
                if action == "start":
                    self.analyze_node_start(element)
                elif action == "end":
                    self.analyze_node_end(element)
        except etree.XMLSyntaxError:
            pass

        if is_tryton_xml and not self._found_tryton_tag:
            # It is a tryton xml file, but the <tryton> tag was not found
            self._diagnostics.append(TrytonTagNotFound(self._filepath))
        if not is_tryton_xml and self._found_tryton_tag:
            # It looks like a tryton xml file, but is not registered
            self._diagnostics.append(TrytonXmlFileUnregistered(self._filepath))
        return self._diagnostics

    def analyze_node_start(self, element: etree.Element) -> None:
        self._tag_stack.append(element)
        if element.text and element.text.startswith("\n"):
            self._cur_prefix = len(element.text) - 1
        analyze_func = getattr(self, f"_analyze_{element.tag}_start", None)
        if analyze_func is None:
            return
        analyze_func(element)

    def analyze_node_end(self, element: etree.Element) -> None:
        analyze_func = getattr(self, f"_analyze_{element.tag}_end", None)
        if analyze_func:
            analyze_func(element)
        self._tag_stack.pop(-1)

    def _analyze_tryton_start(self, node: etree.Element) -> None:
        self._found_tryton_tag = True

    def _analyze_data_start(self, node: etree.Element) -> None:
        if len(self._tag_stack) != 2:
            # We found a <data> node, but not at the second level, so the file
            # does not seem to follow the expected format:
            # <tryton>
            #   <data>
            #       ...
            #   </data>
            # </tryton>
            self.add_diagnostic(
                UnexpectedXMLTag.init_from_analyzer(
                    self, node, tag_name=node.tag
                )
            )
        if not self._module:
            return
        modules = [self._module.get_name()]
        if "depends" in node.attrib:
            # check for extra dependencies
            modules += [x.strip() for x in node.attrib["depends"].split(",")]
        # We have enough information to get a pool going
        self._current_pool = self._pool_manager.get_pool(tuple(modules))

    def _analyze_record_start(self, node: etree.Element) -> None:
        """
        Analyses <record> entries for errors
        """
        if len(self._tag_stack) != 3:
            self.add_diagnostic(
                UnexpectedXMLTag.init_from_analyzer(
                    self, node, tag_name=node.tag
                )
            )
        if "model" not in node.attrib:
            # We expect <record model=...
            self.add_diagnostic(
                RecordMissingAttribute.init_from_analyzer(
                    self, node, attr_name="model"
                )
            )
        if "id" not in node.attrib:
            # Actually, we expect <record model=... id=...
            self.add_diagnostic(
                RecordMissingAttribute.init_from_analyzer(
                    self, node, attr_name="id"
                )
            )
        else:
            if node.attrib["id"] in self._fs_ids:
                # Duplicate id in current module
                self.add_diagnostic(
                    RecordDuplicateId.init_from_analyzer(
                        self,
                        node,
                        fs_id=node.attrib["id"],
                        other_line=str(self._fs_ids[node.attrib["id"]]),
                    )
                )
            else:
                self._fs_ids[node.attrib["id"]] = node.sourceline
        if not self._current_pool or not node.attrib.get("model", None):
            return
        try:
            self._current_model = self._current_pool.get(
                node.attrib["model"], cast("PoolKind", "model")
            )
        except KeyError:
            # The specified model does not exist in the pool for this data bloc
            self.add_diagnostic(
                RecordUnknownModel.init_from_analyzer(
                    self, node, model_name=node.attrib["model"]
                )
            )

    def _analyze_field_start(self, node: etree.Element) -> None:
        """
        Analyses <field> tags
        """
        if len(self._tag_stack) != 4:
            self.add_diagnostic(
                UnexpectedXMLTag.init_from_analyzer(
                    self, node, tag_name=node.tag
                )
            )
        if "name" not in node.attrib:
            # We expect <field name=
            self.add_diagnostic(
                RecordMissingAttribute.init_from_analyzer(
                    self, node, attr_name="name"
                )
            )
        if not self._current_model or "name" not in node.attrib:
            return
        if node.attrib["name"] not in self._current_model.fields:
            # We can check that the name actually matches an existing field
            self.add_diagnostic(
                RecordUnknownField.init_from_analyzer(
                    self,
                    node,
                    model_name=self._current_model.name,
                    field_name=node.attrib["name"],
                )
            )
            return
        if not self._current_pool:
            return
        # TODO: Handle more cases here
        if (
            self._current_model.name == "ir.ui.view"
            and node.attrib["name"] == "model"
        ):
            # Special case because it is a usual case
            try:
                self._current_pool.get(node.text, cast("PoolKind", "model"))
            except KeyError:
                self.add_diagnostic(
                    RecordUnknownModel.init_from_analyzer(
                        self, node, model_name=node.text
                    )
                )

    def _analyze_record_end(self, node: etree.Element) -> None:
        self._current_model = None

    def _analyze_data_end(self, node: etree.Element) -> None:
        self._current_pool = None

    def check_is_tryton_xml_file(self) -> bool:
        """
        Returns whether a xml file is registered in the current tryton module
        """
        with open(self._module_path / "tryton.cfg") as f:
            for line in f.readlines():
                if f"{self._filename}.xml" == line.strip():
                    return True
        return False

    def get_node_position(self, node: etree.Element) -> CodeRange:
        return CodeRange(
            start=CodePosition(line=node.sourceline, column=self._cur_prefix),
            end=CodePosition(line=node.sourceline, column=99),
        )

    def ignored(self, err_code: str, line_number: int) -> bool:
        # Allow to set ignore commentaries above the bad line
        return super().ignored(err_code, line_number) or (
            line_number > 1
            and self._raw_lines[line_number - 2].lstrip().startswith("<!--")
            and f"IGNORE-TRYTON-LS-{err_code}"
            in self._raw_lines[line_number - 2]
        )


class ViewAnalyzer(Analyzer):
    def __init__(
        self, parsed: ParsedViewFile, pool_manager: "PoolManager"
    ) -> None:
        super().__init__(parsed, pool_manager)
        self._ranges: list[CodeRange] = []
        self._module = parsed.get_module()
        assert self._module
        view_info = self._module.get_view_info(parsed.get_filename())
        assert view_info
        modules, view_type, view_model = view_info
        self._current_model = pool_manager.get_pool(modules).get(view_model)
        self._view_type = view_type

    def analyze(
        self, ranges: list[CodeRange] | None = None
    ) -> list[Diagnostic]:
        self._diagnostics = []
        self._ranges = ranges or []
        try:
            for action, element in self._parsed:
                if action == "start":
                    self.analyze_node_start(element)
                elif action == "end":
                    self.analyze_node_end(element)
        except etree.XMLSyntaxError:
            pass
        return self._diagnostics

    def analyze_node_start(self, element: etree.Element) -> None:
        if element.text and element.text.startswith("\n"):
            self._cur_prefix = len(element.text) - 1
        analyze_func = getattr(self, f"_analyze_{element.tag}_start", None)
        if analyze_func is None:
            return
        analyze_func(element)

    def analyze_node_end(self, element: etree.Element) -> None:
        analyze_func = getattr(self, f"_analyze_{element.tag}_end", None)
        if analyze_func:
            analyze_func(element)

    def _analyze_form_start(self, node: etree.Element) -> None:
        if self._view_type not in ("list-form", "form"):
            self.add_diagnostic(
                UnexpectedXMLTag.init_from_analyzer(
                    self, node, tag_name=node.tag
                )
            )

    def _analyze_tree_start(self, node: etree.Element) -> None:
        if self._view_type != "tree":
            self.add_diagnostic(
                UnexpectedXMLTag.init_from_analyzer(
                    self, node, tag_name=node.tag
                )
            )

    def _analyze_data_start(self, node: etree.Element) -> None:
        if self._view_type != "inherit":
            self.add_diagnostic(
                UnexpectedXMLTag.init_from_analyzer(
                    self, node, tag_name=node.tag
                )
            )

    def _analyze_label_start(self, node: etree.Element) -> None:
        self._check_name(node)

    def _analyze_field_start(self, node: etree.Element) -> None:
        self._check_name(node, required=True)

    def _analyze_separator_start(self, node: etree.Element) -> None:
        self._check_name(node)

    def _analyze_group_start(self, node: etree.Element) -> None:
        self._check_name(node)

    def _check_name(self, node: etree.Element, required: bool = False) -> None:
        """
        Check the "name" attribute of a tag, expecting it to be a field of the
        current model
        """
        if "name" not in node.attrib:
            if required:
                self.add_diagnostic(
                    RecordMissingAttribute.init_from_analyzer(
                        self, node, attr_name="name"
                    )
                )
            return
        if not self._current_model:
            return
        if node.attrib["name"] not in self._current_model.fields:
            self.add_diagnostic(
                RecordUnknownField.init_from_analyzer(
                    self,
                    node,
                    model_name=self._current_model.name,
                    field_name=node.attrib["name"],
                )
            )

    def get_node_position(self, node: etree.Element) -> CodeRange:
        return CodeRange(
            start=CodePosition(line=node.sourceline, column=self._cur_prefix),
            end=CodePosition(line=node.sourceline, column=99),
        )
