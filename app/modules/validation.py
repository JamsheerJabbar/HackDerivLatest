import sqlglot
from typing import Optional, Tuple

class ValidationModule:
    """
    HLD 3.5: Validation and Repair Module
    Performs multi-level checks on generated SQL.
    """
    
    def validate(self, sql: str) -> Tuple[bool, Optional[str]]:
        """
        Validates SQL syntax using sqlglot.
        Returns (is_valid, error_message)
        """
        try:
            sqlglot.transpile(sql)
            from app.modules.catalog.templates import assert_select_only
            try:
                assert_select_only(sql)  # generated SQL must be a single SELECT; never UPDATE/DELETE/INSERT/DDL
            except ValueError:
                return False, "Only a single SELECT statement is allowed."
            # Further checks: Schema conformance (mocked here)
            if "DROP" in sql.upper():
                return False, "DROP statements are not allowed."
            return True, None
        except Exception as e:
            return False, str(e)

validation_module = ValidationModule()
