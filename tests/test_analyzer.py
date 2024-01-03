import os
from pathlib import Path

import trytond
from lsprotocol.types import CompletionItemKind

trytond_path = (
    Path(os.path.dirname(os.path.abspath(trytond.__file__))) / "modules"
)
sample_module_path = (
    Path(os.path.dirname(os.path.abspath(__file__))) / "sample_module"
)
if not os.path.exists(trytond_path / "sample_module"):
    (trytond_path / "sample_module").symlink_to(sample_module_path)


def test_diagnostics() -> None:
    expected_errors = [
        # Non registered class
        ("0001", "module.py", 7),
        # Conflicting __name__ declaration
        ("0003", "module.py", 13),
        # Duplicate __name__ declaration
        ("0002", "module.py", 14),
        # Extra super call
        ("1003", "module.py", 19),
        # Super call with different name
        ("1002", "module.py", 21),
        # Access to unknown field (action.whatever)
        ("1007", "module.py", 26),
        # Access to unknown field (m.parent.whatever in loop)
        ("1007", "module.py", 32),
        # Access to unknown field (m.whatever in loop)
        ("1007", "module.py", 33),
        # Access to unknown field (x.whatever in list comprehension)
        ("1007", "module.py", 37),
        # Access to unknown field (w.whatever, infered from tuple unpacking)
        ("1007", "module.py", 45),
        # Access to unknown field (self.whatever)
        ("1007", "module.py", 47),
        # Missing super call in overriden model
        ("1004", "module.py", 54),
        # Missing super call in overriden model
        ("1004", "module.py", 57),
        # Missing super call in overriden model
        ("1004", "module.py", 64),
        # Access to unknown field (self.books[0].whatever in overriden model)
        ("1007", "module.py", 68),
        # Access to unknown field (self.whatever in overriden model)
        ("1007", "module.py", 69),
        # Access to unknown field (x.books[0].whatever in list comprehension)
        ("1007", "module.py", 73),
        # Access to unknown field in fields.depends
        ("1007", "module.py", 85),
        # Access to unknown parent field in depends
        ("1007", "module.py", 97),
        # Access to parent field with a wrong depends in fields.depends
        ("1006", "module.py", 104),
        # Access to unknown parent field in depends (multiple parents)
        ("1007", "module.py", 117),
        # Access to unknown methods in depends
        ("1007", "module.py", 122),
        # Access to dot operator without _parent_ in depends
        ("1007", "module.py", 130),
        # Access to unexisting _parent_ in depends
        ("1007", "module.py", 130),
        # Unknown model in parameter typing
        ("1006", "module.py", 140),
        # Unknown attributes on models from typing annotations in parameters
        ("1007", "module.py", 143),
        ("1007", "module.py", 145),
        ("1007", "module.py", 147),
        ("1007", "module.py", 149),
        # Unknown attribute on variable whose model comes from an annotation
        ("1007", "module.py", 151),
        # Incompatible model detection (from value / annotation)
        ("1008", "module.py", 153),
        ("1008", "module.py", 154),
        # Could not find annotated model in the pool
        ("1006", "module.py", 155),
        # Could not find field on variable whose model comes from an annotation
        ("1007", "module.py", 158),
        # Record not in a data tag
        ("5002", "module.xml", 2),
        # Unknown model in this data block
        ("5004", "module.xml", 4),
        # Missing id attribute
        ("5003", "module.xml", 5),
        # Unknown model in context (custom for ir.ui.view)
        ("5004", "module.xml", 8),
        # Unknown field name in record
        ("5005", "module.xml", 9),
        # Duplicate fs_id in file
        ("5006", "module.xml", 11),
        # Missing id attribute
        ("5003", "module.xml", 22),
        # Registered xml file without a <tryton> tag
        ("5000", "registered_typo.xml", 1),
        # Unknown field in view
        ("5005", "test_view.xml", 3),
        # Unregistered xml file
        ("5001", "unregistered.xml", 1),
    ]
    from tryton_analyzer.pool import PoolManager

    diagnostics = sorted(
        PoolManager().generate_module_diagnostics("sample_module"),
        key=lambda x: (x._filepath.parts[-1], x._position.start.line),
    )

    for (err_code, filename, err_line), diagnostic in zip(
        expected_errors, diagnostics
    ):
        print(diagnostic)
        assert filename == diagnostic._filepath.parts[-1]
        assert err_line == diagnostic._position.start.line
        assert err_code == diagnostic.err_code

    print(diagnostics[len(expected_errors) - 1 :])
    print(expected_errors[len(diagnostics) - 1 :])
    assert len(diagnostics) == len(expected_errors)


def test_completion() -> None:
    from tryton_analyzer.pool import PoolManager

    # Test completion on 'self' in AuthorOverride::test_function
    completions = [
        x
        for x in PoolManager().generate_completions(
            sample_module_path / "module.py", 65, 14
        )
        if x.kind == CompletionItemKind.Field
    ]
    print(completions)
    assert sorted(x.label for x in completions) == [
        "books",
        "create_date",
        "create_uid",
        "id",
        "name",
        "rec_name",
        "write_date",
        "write_uid",
    ]


def test_completion_after_list_subscription() -> None:
    from tryton_analyzer.pool import PoolManager

    # Test completion on 'self' in AuthorOverride::test_function
    completions = [
        x
        for x in PoolManager().generate_completions(
            sample_module_path / "module.py", 67, 24
        )
        if x.kind == CompletionItemKind.Field
    ]
    print(completions)
    assert sorted(x.label for x in completions) == [
        "author",
        "create_date",
        "create_uid",
        "id",
        "name",
        "rec_name",
        "write_date",
        "write_uid",
    ]


def test_completion_in_comprehension() -> None:
    from tryton_analyzer.pool import PoolManager

    # Test completion on 'self' in AuthorOverride::test_function
    completions = [
        x
        for x in PoolManager().generate_completions(
            sample_module_path / "module.py", 72, 22
        )
        if x.kind == CompletionItemKind.Field
    ]
    print(completions)
    assert sorted(x.label for x in completions) == [
        "author",
        "create_date",
        "create_uid",
        "id",
        "name",
        "rec_name",
        "write_date",
        "write_uid",
    ]
