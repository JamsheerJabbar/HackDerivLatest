import os
from typing import List, Dict, Any
from sqlalchemy import create_engine, text, inspect
from app.core.config import settings
from app.core.logger import logger

class DatabaseService:
    def __init__(self):
        kw = {}
        if settings.DATABASE_URL in ("sqlite://", "sqlite:///:memory:"):
            # in-memory SQLite is per-connection: share one connection so worker threads see the same data
            from sqlalchemy.pool import StaticPool
            kw = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
        self.engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, **kw)
        if self.engine.dialect.name == "sqlite":
            from sqlalchemy import event
            from app.core.schema_mapping import register_sqlite_functions
            event.listen(self.engine, "connect", register_sqlite_functions)  # norm_ts() for legacy text timestamps
        self._mapping = None
        self._rewrite_cache: Dict[str, Any] = {}
        logger.info(f"DatabaseService initialized with engine: {settings.DATABASE_URL.split('///')[-1]}")
        self.initialize_db()

    def initialize_db(self):
        """Initialize database schema if it doesn't exist."""
        try:
            inspector = inspect(self.engine)
            if not inspector.has_table("users"):
                print("Initializing database schema...")
                if self.engine.dialect.name == "postgresql":
                    from app.files.migrate_to_postgres import create_schema
                    create_schema(self.engine)
                else:
                    self._execute_sql_file(settings.SCHEMA_PATH)
                print("Schema initialized.")
        except Exception as e:
            print(f"Error initializing database: {e}")

    def _execute_sql_file(self, file_path: str):
        """Execute SQL commands from a file."""
        if not os.path.exists(file_path):
            # Try to resolve relative to project root if needed
            file_path = os.path.join(os.getcwd(), file_path)
            
        if not os.path.exists(file_path):
            print(f"Schema file not found at: {file_path}")
            return

        with open(file_path, 'r', encoding='utf-8') as f:
            sql_content = f.read()
            
        # Split by semicolon for SQLite execution
        # Note: This is a simple split and might fail on complex stored procs, 
        # but works for standard CREATE TABLE/INSERT statements.
        statements = sql_content.split(';')
        
        with self.engine.connect() as conn:
            for statement in statements:
                if statement.strip():
                    try:
                        conn.execute(text(statement))
                    except Exception as e:
                        print(f"Error executing statement: {statement[:50]}... -> {e}")
            conn.commit()

    def execute(self, sql: str) -> List[Dict[str, Any]]:
        """Execute a raw SQL query and return results as a list of dicts."""
        logger.info(f"Executing SQL: {sql}")
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(sql))
                # Check if it's a SELECT query (returns rows)
                if result.returns_rows:
                    data = [dict(row) for row in result.mappings()]
                    logger.info(f"Execution complete. Returned {len(data)} rows.")
                    return data
                else:
                    conn.commit()
                    logger.info(f"Execution complete. Rows affected: {result.rowcount}")
                    return [{"status": "success", "rows_affected": result.rowcount}]
        except Exception as e:
            logger.error(f"DATABASE EXECUTION ERROR: {str(e)} | Query: {sql}")
            return []

    def execute_params(self, sql: str, bind: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Execute a single SELECT with bound parameters. Unlike execute(), errors are raised."""
        import sqlglot
        from sqlglot import exp
        trees = sqlglot.parse(sql)
        if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Union)):
            raise ValueError("only a single SELECT statement is allowed")
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), bind or {}).mappings()]

    # ── logical (catalog) SQL -> whatever physical schema this database has ──────────
    @property
    def mapping(self):
        """SchemaMapping for this database (introspected once; call reset_mapping() after schema changes)."""
        if self._mapping is None:
            from app.core.schema_mapping import mapping_for_engine
            self._mapping = mapping_for_engine(self.engine)
            logger.info(f"Schema mapping profile: {self._mapping.profile_name}")
        return self._mapping

    def reset_mapping(self) -> None:
        self._mapping = None
        self._rewrite_cache.clear()

    def execute_logical(self, sql: str, bind: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Execute catalog SQL written against the canonical schema: rewrite to the physical schema, then
        run it as a bound SELECT. Raises UnavailableColumn when the database lacks something the SQL needs."""
        rw = self._rewrite_cache.get(sql)
        if rw is None:
            rw = self._rewrite_cache[sql] = self.mapping.rewrite_sql(sql)
        return self.execute_params(rw.sql, rw.apply_bind(bind))

    def available_metric_ids(self, catalog: Dict[str, Any]):
        """Catalog metric ids that can run on this database (others lack tables/columns)."""
        return self.mapping.available_ids(catalog)

    def unavailable_metric_reasons(self, catalog: Dict[str, Any]) -> Dict[str, str]:
        return self.mapping.unavailable_reasons(catalog)

    def get_schema_info(self) -> List[str]:
        """Returns a list of 'table.column' strings for all tables in the database."""
        schema_info = []
        try:
            inspector = inspect(self.engine)
            tables = inspector.get_table_names()
            for table in tables:
                columns = inspector.get_columns(table)
                for col in columns:
                    schema_info.append(f"{table}.{col['name']}")
            return schema_info
        except Exception as e:
            logger.error(f"Error fetching schema info: {e}")
            return []

db_service = DatabaseService()
