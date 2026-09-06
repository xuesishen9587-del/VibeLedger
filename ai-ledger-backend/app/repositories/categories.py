from typing import Optional, Dict, Any, List
from uuid import UUID, uuid4

class FallbackCategoryArchivedError(Exception):
    """Raised when attempting to archive or deactivate a fallback category."""
    pass

def create_category(
    conn,
    household_id: UUID,
    name: str,
    category_type: str,
    description: Optional[str] = None,
    is_fallback: bool = False,
    category_id: Optional[UUID] = None,
    status: str = 'active'
) -> Dict[str, Any]:
    if category_id is None:
        category_id = uuid4()
    elif isinstance(category_id, str):
        category_id = UUID(category_id)

    if isinstance(household_id, str):
        household_id = UUID(household_id)

    if category_type not in ("expense", "income"):
        raise ValueError(f"Invalid category_type: {category_type}")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO categories (
                id, household_id, name, category_type, description, is_fallback, status, row_version, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, now(), now())
            RETURNING id, household_id, name, category_type, description, is_fallback, status, row_version, created_at, updated_at;
            """,
            (category_id, household_id, name.strip(), category_type, description, is_fallback, status)
        )
        row = cur.fetchone()
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "category_type": row[3],
            "description": row[4],
            "is_fallback": row[5],
            "status": row[6],
            "row_version": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }

def get_category(conn, category_id: UUID, household_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    query = """
        SELECT id, household_id, name, category_type, description, is_fallback, status, row_version, created_at, updated_at
        FROM categories
        WHERE id = %s
    """
    params: List[Any] = [category_id]
    if household_id is not None:
        query += " AND household_id = %s"
        params.append(household_id)

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "category_type": row[3],
            "description": row[4],
            "is_fallback": row[5],
            "status": row[6],
            "row_version": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }

def get_fallback_category(conn, household_id: UUID, category_type: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, household_id, name, category_type, description, is_fallback, status, row_version, created_at, updated_at
            FROM categories
            WHERE household_id = %s AND category_type = %s AND is_fallback = true AND status = 'active';
            """,
            (household_id, category_type)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "category_type": row[3],
            "description": row[4],
            "is_fallback": row[5],
            "status": row[6],
            "row_version": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }

def list_categories(
    conn,
    household_id: UUID,
    category_type: Optional[str] = None,
    status: Optional[str] = None
) -> List[Dict[str, Any]]:
    query = """
        SELECT id, household_id, name, category_type, description, is_fallback, status, row_version, created_at, updated_at
        FROM categories
        WHERE household_id = %(household_id)s
    """
    params: Dict[str, Any] = {"household_id": household_id}

    if category_type is not None:
        query += " AND category_type = %(category_type)s"
        params["category_type"] = category_type
    if status is not None:
        query += " AND status = %(status)s"
        params["status"] = status

    query += " ORDER BY category_type ASC, is_fallback ASC, name ASC;"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        categories = []
        for r in rows:
            categories.append({
                "id": r[0],
                "household_id": r[1],
                "name": r[2],
                "category_type": r[3],
                "description": r[4],
                "is_fallback": r[5],
                "status": r[6],
                "row_version": r[7],
                "created_at": r[8],
                "updated_at": r[9],
            })
        return categories

def check_category_name_exists(
    conn,
    household_id: UUID,
    category_type: str,
    name: str,
    exclude_category_id: Optional[UUID] = None
) -> bool:
    query = """
        SELECT 1 FROM categories
        WHERE household_id = %s
          AND category_type = %s
          AND lower(name) = lower(%s)
          AND status = 'active'
    """
    params: List[Any] = [household_id, category_type, name.strip()]
    if exclude_category_id is not None:
        query += " AND id <> %s"
        params.append(exclude_category_id)
    query += " LIMIT 1;"

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        return cur.fetchone() is not None

def update_category(
    conn,
    household_id: UUID,
    category_id: UUID,
    name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    expected_version: Optional[int] = None,
    fields_set: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    set_clauses = ["updated_at = now()"]
    params: List[Any] = []

    if name is not None:
        set_clauses.append("name = %s")
        params.append(name.strip())

    if fields_set is not None:
        if "description" in fields_set:
            set_clauses.append("description = %s")
            params.append(description.strip() if description else None)
        if "status" in fields_set and status is not None:
            set_clauses.append("status = %s")
            params.append(status)
    else:
        if description is not None:
            set_clauses.append("description = %s")
            params.append(description.strip())
        if status is not None:
            set_clauses.append("status = %s")
            params.append(status)

    set_clauses.append("row_version = row_version + 1")

    where_clauses = ["household_id = %s", "id = %s"]
    params.extend([household_id, category_id])

    if expected_version is not None:
        where_clauses.append("row_version = %s")
        params.append(expected_version)

    query = f"""
        UPDATE categories
        SET {', '.join(set_clauses)}
        WHERE {' AND '.join(where_clauses)}
        RETURNING id, household_id, name, category_type, description, is_fallback, status, row_version, created_at, updated_at;
    """

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "category_type": row[3],
            "description": row[4],
            "is_fallback": row[5],
            "status": row[6],
            "row_version": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }

def deactivate_category(
    conn,
    household_id: UUID,
    category_id: UUID,
    expected_version: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Deactivates a category. Enforces that fallback categories CANNOT be archived/deactivated.
    """
    # Check if category is fallback
    cat = get_category(conn, category_id, household_id)
    if not cat:
        return None
    if cat.get("is_fallback"):
        raise FallbackCategoryArchivedError("Fallback category cannot be archived or deactivated.")

    query = """
        UPDATE categories
        SET status = 'inactive',
            row_version = row_version + 1,
            updated_at = now()
        WHERE household_id = %s AND id = %s AND status = 'active'
    """
    params: List[Any] = [household_id, category_id]
    if expected_version is not None:
        query += " AND row_version = %s"
        params.append(expected_version)

    query += " RETURNING id, household_id, name, category_type, description, is_fallback, status, row_version, created_at, updated_at;"

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "category_type": row[3],
            "description": row[4],
            "is_fallback": row[5],
            "status": row[6],
            "row_version": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }
