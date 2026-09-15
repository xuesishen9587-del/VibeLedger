"""SQL for independent spending records. Every financial query is household scoped."""
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


def rows(conn, query, params=()):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def get(conn, household_id, transaction_id):
    result = rows(conn, "SELECT * FROM transactions WHERE household_id=%s AND id=%s",
                  (household_id, transaction_id))
    return result[0] if result else None


def insert(conn, values):
    query = sql.SQL("INSERT INTO transactions ({}) VALUES ({}) RETURNING *").format(
        sql.SQL(",").join(map(sql.Identifier, values)),
        sql.SQL(",").join(sql.Placeholder() for _ in values))
    return rows(conn, query, tuple(values.values()))[0]


def update(conn, household_id, transaction_id, values):
    query = sql.SQL("UPDATE transactions SET {}, row_version=row_version+1, updated_at=now() "
                    "WHERE household_id=%s AND id=%s RETURNING *").format(
        sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(key)) for key in values))
    return rows(conn, query, (*values.values(), household_id, transaction_id))[0]


def refund_total(conn, household_id, transaction_id, exclude_id=None):
    return rows(conn, "SELECT COALESCE(SUM(original_amount),0) AS total FROM transactions "
                "WHERE household_id=%s AND refund_of_transaction_id=%s AND status='committed' "
                "AND (%s::uuid IS NULL OR id<>%s)",
                (household_id, transaction_id, exclude_id, exclude_id))[0]["total"]


def list_records(conn, household_id, filters, cursor=None, limit=50):
    predicates = ["household_id=%s"]
    params = [household_id]
    for key in ("transaction_type", "account_id", "category_id", "status"):
        if filters.get(key) is not None:
            predicates.append(f"{key}=%s")
            params.append(filters[key])
    for key, op in (("from", ">="), ("to", "<=")):
        if filters.get(key):
            predicates.append(f"occurred_on{op}%s")
            params.append(filters[key])
    if filters.get("merchant") is not None:
        predicates.append("merchant_normalized=%s")
        params.append(" ".join(filters["merchant"].casefold().split()))
    for key, predicate in (
        ("missing_account", "account_id IS NULL"),
        ("category_uncertain", "category_uncertain"),
        ("needs_metadata_review", "((account_id IS NULL AND NOT account_review_acknowledged) OR category_uncertain)"),
    ):
        if filters.get(key) is not None:
            predicates.append(f"({predicate})=%s")
            params.append(filters[key])
    if cursor:
        predicates.append("(occurred_on,id)<(%s,%s)")
        params.extend(cursor)
    query = "SELECT * FROM transactions WHERE " + " AND ".join(predicates)
    return rows(conn, query + " ORDER BY occurred_on DESC,id DESC LIMIT %s", (*params, limit + 1))


def duplicate_candidates(conn, household_id, record):
    return rows(conn, "SELECT * FROM transactions WHERE household_id=%s AND status='committed' "
                "AND transaction_type='expense' AND occurred_on=%s AND original_amount=%s "
                "AND original_currency=%s AND merchant_normalized IS NOT DISTINCT FROM %s "
                "AND (account_id=%s OR account_id IS NULL OR %s::uuid IS NULL) ORDER BY id",
                (household_id, record["occurred_on"], record["original_amount"],
                 record["original_currency"], record.get("merchant_normalized"),
                 record.get("account_id"), record.get("account_id")))
