from trytond.pool import Pool

from . import module


def register():
    Pool.register(
        module.Author,
        module.Book,
        module.Menu,
        module="sample_module",
        type_="model",
    )

    Pool.register(
        module.AuthorOverride,
        module.BookOverride,
        module="sample_module",
        type_="model",
        depends=["res"],
    )
