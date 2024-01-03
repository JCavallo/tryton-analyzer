from trytond.analyzer import Record, Records
from trytond.model import ModelSQL, ModelView, fields
from trytond.pool import Pool, PoolMeta


class NonRegistered(ModelSQL):
    __name__ = "whatever"


class Author(ModelSQL, ModelView):
    "Author"
    __name__ = "author"
    __name__ = "uthor"
    __name__ = "author"

    name = fields.Char("Author")
    books = fields.One2Many("book", "author", "Books")

    def test_function(self):
        super().test_function()
        super().tes_function()
        pool = Pool()
        Action = pool.get("ir.action")
        Menu = Pool().get("ir.ui.menu")
        action = Action()
        action.groups
        action.name
        menus = Menu.browse()
        for m in menus:
            m.action
            m.parent.action
            m.parent.whatever
            m.whatever

        [x.action for x in menus]
        [x.whatever for x in menus]  # IGNORE-TRYTON-LS-1007
        [x.whatever for x in menus]
        # IGNORE-TRYTON-LS-1007
        [x.parent.whatever >= 0 for x in menus]
        [x.parent.action for x in menus]

        (w,) = menus
        w.action.whatever
        w.action
        w.whatever

        self.whatever
        self.name


class Menu(metaclass=PoolMeta):
    __name__ = "ir.ui.menu"

    def create(self):
        pass

    def pre_validate(self):
        pass


class AuthorOverride(metaclass=PoolMeta):
    __name__ = "author"

    def test_function(self):
        self.name
        self.books
        self.books[0].author
        self.books[0].whatever
        self.whatever

        records = self.search([])
        [x.books[0].author for x in records]
        [x.books[0].whatever for x in records]

        Action = Pool().get("ir.action")
        action = Action()
        # This is ok because AuthorOverride has an extra depends on module res
        action.groups

    def get_rec_name(self, name) -> str:
        # No missing super call here because the definition in tryton has a
        # IGNORE-TRYTON-LS-1004
        return ""

    @fields.depends("name", "whatever")
    def test_depends(self):
        pass


class Book(ModelSQL):
    "Book"
    __name__ = "book"

    name = fields.Char("Name")
    author = fields.Many2One("author", "Author")

    @fields.depends("name", "_parent_author.name", "_parent_author.whatever")
    def test_depends(self):
        pass

    @fields.depends(
        "name",
        "_parent_author.name",
        "_parent_author._parent_create_uid.login",
    )
    def test_depends_1(self):
        pass


class BookOverride(metaclass=PoolMeta):
    __name__ = "book"

    @fields.depends(
        "name",
        "_parent_author.name",
        "_parent_author._parent_create_uid.login",
        "_parent_author._parent_create_uid.whatever",
    )
    def test_depends_2(self):
        pass

    @fields.depends(methods=["test_typo"])
    def test_depends_4(self):
        pass

    @fields.depends(methods=["test_depends"])
    def test_depends_5(self):
        pass

    @fields.depends("author.whatever", "_parent_whatever.name")
    def test_depends_6(self):
        pass

    def test_param_typing(
        self,
        self_record: Record,
        self_records: Records,
        other_record: Record["ir.model"],
        other_records: Records["ir.model"],
        bad_model: Record["whatever"],
    ):
        self_record.name
        self_record.whatever
        [x.name for x in self_records]
        [x.whatever for x in self_records]
        other_record.fields
        other_record.whatever
        [x.fields for x in other_records]
        [x.whatever for x in other_records]
        test_annotate_variable: Record = ...
        test_annotate_variable.whatever
        test_annotate_variable.name
        test_variable_model_override: Record = other_record
        other_test_annotate: Record["ir.model"] = self
        another_test_annotate: Record["ir.whatever"] = self
        final_test_annotate: Record["ir.model"] = ...
        final_test_annotate.fields[0].name
        final_test_annotate.fields[0].whatever
