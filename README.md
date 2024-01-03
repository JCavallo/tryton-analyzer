# Tryton Analyzer

## What's this

`tryton-analyzer` is a dedicated Language Server / Linter to assist developers
using the [tryton](https://www.tryton.org/) framework.

Due to how the framework operates (most notably the runtime inheritance), the
standard Python tooling is not as useful as it can / should be. This means that
most errors that could be detected by a linter (unknown attributes, etc.)
are not, which leads to runtime / production errors.

## Disclaimer

This is just a hobby project for now, and though it is already useful, it
requires a patched trytond (on version 6.8).

It is only tested on Neovim, though it should work on other editors supporting
the Language Server Protocol.

## How does it work

It uses [pygls](https://pygls.readthedocs.io/en/latest/) to handle the language
server protocol, [libcst](https://libcst.readthedocs.io/en/latest) to parse the
sources, and a [patched
trytond](https://github.com/jcavallo/tryton-core/tree/db_less_pools_68) to be able to
load modules without any database whatsoever.

The language server itself spawns and requests informations from separate `trytond`
processes to be able to survive Syntax Errors in the source code.

## What can it do

Focus so far has been on basic things:
- Detecting unkown attributes (python / xml)
- Completion
- Support of `extras_depend` to distinguish available models / fields even in a
given module

Model detection is done through parsing of the syntax tree using `libsct`. It
relies on:
- `self` / `cls` for obvious reasons
- Special function parameters (for instance, the second argument of `validate`
    will always be a list of records)
- Parsing of `Pool().get` calls
- Custom type annotations: `def my_function(self, invoices:
    Records["account.invoice"])`, though this does not play well with other
linters who interpret `"account.invoice"` as a type that obviously does not
exist

There are a few very specific checks that are implemented because of past
trauma :)
- Do not call super on a function without a parent
- Forgetting a super call

Screenshots:

![Test module diagnostics 1](/doc/images/sample_module_1.png?raw=true)
![Test module diagnostics 2](/doc/images/sample_module_2.png?raw=true)
![Test module diagnostics 3](/doc/images/sample_module_3.png?raw=true)
![Test module diagnostics 4](/doc/images/sample_module_4.png?raw=true)
![Test module diagnostics 5](/doc/images/sample_module_5.png?raw=true)
![Test module completion](/doc/images/sample_module_completion.png?raw=true)

## What will it do

I have a lot of things I would like to do next:
- Global ignore list via tox.ini
- Return type annotations
- Extract more informations from the tryton pool (identify `getters` & co to 
    automatically assign types to parameters, identify from super calls)
- Domain / state parsing
- Jump to definition
- Better information for fields during completion
- Find usages of fields / methods (though this one will probably be difficult
    and need some sort of persistent caching)
- Detect register / depends missing a extras_depend

## Give it a try

`pip install tryton-analyzer`, and setup your development environment to use
the [patched trytond](https://github.com/jcavallo/tryton-core/tree/db_less_pools_68)
rather than the default one.

On the client side, use [lspconfig](https://github.com/neovim/nvim-lspconfig) for Neovim:
```lua
  local lspconfig = require("lspconfig")
  local configs = require'lspconfig.configs'
  if not configs.tryton_analyzer then
    configs.tryton_analyzer = {
      default_config = {
        cmd = { 'tryton-ls' },
        filetypes = {'python', 'xml'},
        root_dir = lspconfig.util.root_pattern(".git"),
        settings = {},
      };
    }
  end
  lspconfig.tryton_analyzer.setup{}
```
You should be good to go.

The `tryton-lint` script allows to lint a tryton module as a whole.
