import importlib
import io
import unittest
from unittest.mock import patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models import Document

migration = importlib.import_module(
    "app.db.migrations.versions.b7c2d9e4f610_add_document_indexing_metadata"
)
FIELDS = {
    "embedding_model",
    "embedding_dimensions",
    "embedding_tokens",
    "indexed_at",
    "indexing_error_code",
}


class IndexingMigrationTests(unittest.TestCase):
    def test_postgres_sql_and_orm_metadata(self):
        output = io.StringIO()
        context = MigrationContext.configure(
            dialect_name="postgresql",
            opts={"as_sql": True, "literal_binds": True, "output_buffer": output},
        )
        with patch.object(migration, "op", Operations(context)):
            migration.upgrade()
        sql = output.getvalue()
        self.assertEqual(sql.count("ALTER TABLE documents ADD COLUMN"), 5)
        self.assertIn("TIMESTAMP WITH TIME ZONE", sql)
        self.assertIn(
            "UPDATE documents SET status = 'pending' WHERE status = 'ready'", sql
        )
        for name in FIELDS:
            self.assertTrue(Document.__table__.c[name].nullable)
        self.assertTrue(Document.__table__.c.indexed_at.type.timezone)

    def test_existing_records_survive_and_metadata_starts_unknown(self):
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "CREATE TABLE documents (id INTEGER PRIMARY KEY, status VARCHAR(50) NOT NULL)"
                    )
                )
                connection.execute(
                    sa.text(
                        "CREATE TABLE document_chunks (id INTEGER PRIMARY KEY, document_id INTEGER, content TEXT)"
                    )
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO documents VALUES (1, 'ready'), (2, 'failed'), (3, 'processing'), (4, 'pending')"
                    )
                )
                connection.execute(
                    sa.text("INSERT INTO document_chunks VALUES (1, 1, 'Source text')")
                )
                with patch.object(
                    migration, "op", Operations(MigrationContext.configure(connection))
                ):
                    migration.upgrade()
                statuses = connection.execute(
                    sa.text("SELECT id,status FROM documents ORDER BY id")
                ).all()
                self.assertEqual(
                    statuses,
                    [(1, "pending"), (2, "failed"), (3, "processing"), (4, "pending")],
                )
                self.assertEqual(
                    connection.execute(
                        sa.text("SELECT content FROM document_chunks")
                    ).scalar(),
                    "Source text",
                )
                values = connection.execute(
                    sa.text(
                        "SELECT embedding_model, embedding_dimensions, embedding_tokens, indexed_at, indexing_error_code FROM documents WHERE id=1"
                    )
                ).one()
                self.assertEqual(tuple(values), (None,) * 5)
                with patch.object(
                    migration, "op", Operations(MigrationContext.configure(connection))
                ):
                    migration.downgrade()
                names = {
                    col["name"]
                    for col in sa.inspect(connection).get_columns("documents")
                }
                self.assertEqual(names, {"id", "status"})
                self.assertEqual(
                    connection.execute(
                        sa.text("SELECT count(*) FROM documents")
                    ).scalar(),
                    4,
                )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
