#!/usr/bin/env python3
import re
import sys
from pathlib import Path

from libcst.metadata import CodeRange
from lsprotocol.types import (
    TEXT_DOCUMENT_COMPLETION,
    TEXT_DOCUMENT_DID_CHANGE,
    TEXT_DOCUMENT_DID_OPEN,
    TEXT_DOCUMENT_DID_SAVE,
    CompletionItem,
    CompletionList,
    CompletionParams,
    DidChangeTextDocumentParams,
    DidOpenTextDocumentParams,
    DidSaveTextDocumentParams,
    Position,
    Range,
    TextDocumentContentChangeEvent_Type1,
    TextDocumentIdentifier,
)
from pygls.server import LanguageServer
from pygls.workspace import Document

from .pool import PoolManager


def log_debug(log: str) -> None:
    # TODO: How to make the lsp client display messages
    print(log, file=sys.stderr)


class TrytonServer(LanguageServer):
    """
    The actual Language Server (using the pygls tooling)
    """

    def __init__(self) -> None:
        super().__init__("tryton-ls", "v0.7")
        # Tryton pool interface. If the actual tryton pool cannot be loaded (or
        # if we want to load an up to date pool), it can be dropped and it will
        # be respawned when needed
        self._pool_manager: PoolManager | None = None
        # Cache for completions
        self._last_complete_position: tuple[int, int] = (0, 0)
        self._last_completion: list[CompletionItem] | None = None

    def get_pool_manager(self) -> PoolManager:
        """
        Returns a PoolManager to query Tryton for informations
        """
        if self._pool_manager is None:
            self._pool_manager = PoolManager()
        return self._pool_manager

    def reset_pool_manager(self) -> None:
        """
        Clears all persistent informations from the Language Server, so a fresh
        interface can be spawned
        """
        if self._pool_manager is None:
            return
        self._pool_manager.close()
        self._pool_manager = None
        self._last_complete_position = (0, 0)
        self._last_completion = None

    def generate_diagnostics(self, uri: str, ranges: list[Range]) -> None:
        """
        Entrypoint for LSP diagnostics. Transforms the inputs which are pygls
        types into Tryton / Libcst types
        """
        log_debug(f"starting diagnostic generation for {uri}")
        text_document = self.workspace.get_document(uri)
        source_data = text_document.source
        document_path = Path(text_document.path)
        diagnostics = self.get_pool_manager().generate_diagnostics(
            document_path,
            data=source_data,
            ranges=[
                CodeRange(
                    (range.start.line, range.start.character),
                    (range.end.line, range.end.character),
                )
                for range in ranges
            ],
        )
        self.publish_diagnostics(
            uri, [x.to_lsp_diagnostic() for x in diagnostics]
        )
        log_debug(f"Completed diagnostic generation for {uri}")

    def generate_completions(
        self, document: TextDocumentIdentifier, position: Position
    ) -> CompletionList:
        """
        Entrypoint for LSP completions. Transforms the inputs which are pygls
        types into Tryton / Libcst types.
        Uses a cache to avoid recomputing everything everytime a character is
        entered, and prefilters based on non-validated chars
        """
        text_document = self.workspace.get_document(document.uri)
        completion_data = self._get_completion_data(text_document, position)
        log_debug(
            f"Starting completion for {document.uri}, filter {completion_data.filter}"
        )
        if completion_data.position == self._last_complete_position:
            log_debug(f"Used cached completion for {document.uri}")
        else:
            self._last_completion_position = completion_data.position
            document_path = Path(text_document.path)
            self._last_completion = (
                self.get_pool_manager().generate_completions(
                    document_path,
                    data=completion_data.source,
                    line=position.line + 1,
                    column=position.character,
                )
            )
        completions = self._last_completion
        if not completions:
            return CompletionList(is_incomplete=False, items=[])
        incomplete = False
        if completion_data.filter:
            # Stupid implementation of fuzzy searching
            target_regex = re.compile(
                r".*".join(re.escape(x) for x in completion_data.filter)
            )
            completions = [
                x for x in completions if re.search(target_regex, x.label)
            ]
        if len(completions) > 100:
            completions = completions[:100]
            incomplete = True
        result = CompletionList(is_incomplete=False, items=completions)
        log_debug(
            f"{'[PARTIAL] ' if incomplete else ''}Completed completion "
            f"for {document.uri} with {len(completions)} items"
        )
        return result

    def _get_completion_data(
        self, text_document: Document, position: Position
    ) -> "CompletionData":
        """
        Finds the position where we want to start the completion.
        We are looking for the type of the variable right before the last ".",
        so we adjust the content of the last line to remove everything after
        (including) the dot to have somthing that can be parsed.
        """
        lines = [x[:-1] for x in text_document.lines]
        line_data = lines[position.line]
        col = position.character - 1
        if line_data[col] == ".":
            lines[position.line] = (
                line_data[: col + 1] + "a" + line_data[col + 1 :]
            )
            col += 1
        for i in range(col):
            if line_data[col - i] == ".":
                col = col - i + 1
                break
        return CompletionData(
            "\n".join(lines), (position.line + 1, col), line_data[col:]
        )


class CompletionData:
    def __init__(self, source: str, position: tuple[int, int], filter: str):
        super().__init__()
        self.source = source
        self.position = position
        self.filter = filter


def run() -> None:
    tryton_server = TrytonServer()

    @tryton_server.feature(
        TEXT_DOCUMENT_COMPLETION,
    )
    async def completions(
        params: CompletionParams | None = None,
    ) -> CompletionList | None:
        """Returns completion items."""
        if not params:
            return None
        return tryton_server.generate_completions(
            params.text_document, params.position
        )

    @tryton_server.feature(TEXT_DOCUMENT_DID_OPEN)
    async def did_open(
        ls: LanguageServer, params: DidOpenTextDocumentParams
    ) -> None:
        """Text document did open notification."""
        tryton_server.generate_diagnostics(params.text_document.uri, [])

    @tryton_server.feature(TEXT_DOCUMENT_DID_SAVE)
    async def did_save(
        ls: LanguageServer, params: DidSaveTextDocumentParams
    ) -> None:
        """Text document did change notification."""
        # On save we want to reset the server so that we have up to date
        # informations
        tryton_server.reset_pool_manager()
        tryton_server.generate_diagnostics(params.text_document.uri, [])

    @tryton_server.feature(TEXT_DOCUMENT_DID_CHANGE)
    async def did_change(
        ls: LanguageServer, params: DidChangeTextDocumentParams
    ) -> None:
        """Text document did change notification."""
        # While the document is changed but not saved, we only update the
        # diagnostics for the current function for performances. Saving will
        # trigger a full diagnostic of the file
        ranges = []
        for content_change in params.content_changes:
            if (
                isinstance(
                    content_change, TextDocumentContentChangeEvent_Type1
                )
                and content_change.range
            ):
                ranges.append(content_change.range)
        if ranges:
            tryton_server.generate_diagnostics(
                params.text_document.uri, ranges
            )

    tryton_server.start_io()


if __name__ == "__main__":
    run()
