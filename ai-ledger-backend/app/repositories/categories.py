from typing import Optional, Dict, Any, List
from uuid import UUID

def create_category(
    conn,
    category_id: UUID,
    household_id: UUID,
    name: str,
    category_type: str,
    status: str = 'active'
) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO categories (id, household_id, name, category_type, status)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, household_id, name, category_type, status, created_at, updated_at;
            """,
            (category_id, household_id, name.strip(), category_type, status)
        )
        row = cur.fetchone()
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "category_type": row[3],
            "status": row[4],
            "created_at": row[5],
            "updated_at": row[6]
        }

def get_category(conn, category_id: UUID, household_id: Optional[UUID] = None) -> Optional[Dict[str, Any]]:
    query = """
        SELECT id, household_id, name, category_type, status, created_at, updated_at
        FROM categories
        WHERE id = %s
    """
    params = [category_id]
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
            "status": row[4],
            "created_at": row[5],
            "updated_at": row[6]
        }

def list_categories(
    conn,
    household_id: UUID,
    category_type: Optional[str] = None,
    status: Optional[str] = None
) -> List[Dict[str, Any]]:
    query = """
        SELECT id, household_id, name, category_type, status, created_at, updated_at
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

    query += " ORDER BY name ASC;"

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
                "status": r[4],
                "created_at": r[5],
                "updated_at": r[6]
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
    params = [household_id, category_type, name.strip()]
    if exclude_category_id is not None:
        query += " AND id <> %s"
        params.append(exclude_category_id)
    query += " LIMIT 1;"

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        return cur.fetchone() is not None

def update_category(conn, category_id: UUID, name: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE categories
            SET name = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING id, household_id, name, category_type, status, created_at, updated_at;
            """,
            (name.strip(), category_id)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "category_type": row[3],
            "status": row[4],
            "created_at": row[5],
            "updated_at": row[6]
        }

def deactivate_category(conn, category_id: UUID) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE categories
            SET status = 'inactive',
                updated_at = now()
            WHERE id = %s
            RETURNING id, household_id, name, category_type, status, created_at, updated_at;
            """,
            (category_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "household_id": row[1],
            "name": row[2],
            "category_type": row[3],
            "status": row[4],
            "created_at": row[5],
            "updated_at": row[6]
        }
