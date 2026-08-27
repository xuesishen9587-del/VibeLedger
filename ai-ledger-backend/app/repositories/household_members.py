from typing import Optional, Dict, Any, List
from uuid import UUID
from datetime import date

def list_active_household_memberships_for_user(conn, user_id: UUID) -> List[Dict[str, Any]]:
    """
    Returns all active household memberships for a user where the associated household is active.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT hm.household_id, hm.user_id, hm.role, hm.joined_at, h.name AS household_name, h.status AS household_status
            FROM household_members hm
            JOIN households h ON h.id = hm.household_id
            WHERE hm.user_id = %s
              AND h.status = 'active'
            ORDER BY hm.joined_at ASC;
            """,
            (user_id,)
        )
        rows = cur.fetchall()
        return [
            {
                "household_id": r[0],
                "user_id": r[1],
                "role": r[2],
                "joined_at": r[3],
                "household_name": r[4],
                "household_status": r[5],
            }
            for r in rows
        ]

def get_household_membership(conn, household_id: UUID, user_id: UUID) -> Optional[Dict[str, Any]]:
    """
    Looks up a specific user's membership in a given household.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT hm.household_id, hm.user_id, hm.role, hm.joined_at, h.status AS household_status
            FROM household_members hm
            JOIN households h ON h.id = hm.household_id
            WHERE hm.household_id = %s AND hm.user_id = %s;
            """,
            (household_id, user_id)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "household_id": row[0],
            "user_id": row[1],
            "role": row[2],
            "joined_at": row[3],
            "household_status": row[4],
        }

def add_household_member(
    conn,
    household_id: UUID,
    user_id: UUID,
    role: str = "member",
) -> None:
    """
    Adds a user to a household with a specified role ('owner' or 'member').
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO household_members (household_id, user_id, role, joined_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (household_id, user_id) DO UPDATE SET role = EXCLUDED.role;
            """,
            (household_id, user_id, role)
        )

def create_household(
    conn,
    household_id: UUID,
    name: str,
    reporting_currency: str = "CNY",
    ledger_start_date: Optional[date] = None,
    status: str = "active",
) -> Dict[str, Any]:
    """
    Creates a household record (primarily for provisioning and test fixtures).
    """
    start_date = ledger_start_date or date(2026, 1, 1)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO households (
                id, name, reporting_currency, ledger_start_date, status, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, now(), now())
            RETURNING id, name, reporting_currency, ledger_start_date, status, created_at, updated_at;
            """,
            (household_id, name, reporting_currency, start_date, status)
        )
        row = cur.fetchone()
        return {
            "id": row[0],
            "name": row[1],
            "reporting_currency": row[2],
            "ledger_start_date": row[3],
            "status": row[4],
            "created_at": row[5],
            "updated_at": row[6],
        }
