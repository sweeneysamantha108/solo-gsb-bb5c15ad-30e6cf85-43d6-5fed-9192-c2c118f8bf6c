from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import psycopg

from .config import settings
from .errors import ApiError, conflict, not_found
from .models import (
    AgreementIn,
    PeriodIn,
    PlayIn,
    RightsholderIn,
    WorkIn,
)
from .split import Share, split_amount

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_one(conn, sql: str, params: tuple = ()) -> Optional[dict]:
    return conn.execute(sql, params).fetchone()


def _get_work_by_code(conn, work_code: str) -> dict:
    row = _fetch_one(conn, "SELECT * FROM works WHERE code = %s", (work_code,))
    if row is None:
        raise not_found("work", work_code)
    return row


def _get_rightsholder_by_code(conn, code: str) -> dict:
    row = _fetch_one(
        conn, "SELECT * FROM rightsholders WHERE code = %s", (code,)
    )
    if row is None:
        raise not_found("rightsholder", code)
    return row


def _content_hash(work_id: int, occurred_at: datetime, amount: int) -> str:
    payload = json.dumps(
        {
            "work_id": work_id,
            "occurred_at": occurred_at.isoformat(),
            "amount": amount,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _unique_violation(exc: Exception) -> bool:
    return isinstance(exc, psycopg.IntegrityError) and getattr(
        exc, "sqlstate", ""
    ) == "23505"


# ---------------------------------------------------------------------------
# Rightsholders / works
# ---------------------------------------------------------------------------


def create_rightsholder(conn, body: RightsholderIn) -> tuple[dict, bool]:
    existing = _fetch_one(
        conn, "SELECT * FROM rightsholders WHERE code = %s", (body.code,)
    )
    if existing is not None:
        if existing["name"] != body.name:
            raise conflict(
                "rightsholder_code_conflict",
                f"rightsholder code {body.code} already exists with different name",
            )
        return existing, False
    row = _fetch_one(
        conn,
        "INSERT INTO rightsholders (code, name) VALUES (%s, %s) RETURNING *",
        (body.code, body.name),
    )
    return row, True


def list_rightsholders(conn) -> list[dict]:
    return conn.execute(
        "SELECT * FROM rightsholders ORDER BY code"
    ).fetchall()


def create_work(conn, body: WorkIn) -> tuple[dict, bool]:
    existing = _fetch_one(conn, "SELECT * FROM works WHERE code = %s", (body.code,))
    if existing is not None:
        if existing["title"] != body.title:
            raise conflict(
                "work_code_conflict",
                f"work code {body.code} already exists with different title",
            )
        return existing, False
    row = _fetch_one(
        conn,
        "INSERT INTO works (code, title) VALUES (%s, %s) RETURNING *",
        (body.code, body.title),
    )
    return row, True


def list_works(conn) -> list[dict]:
    return conn.execute("SELECT * FROM works ORDER BY code").fetchall()


# ---------------------------------------------------------------------------
# Agreements
# ---------------------------------------------------------------------------

_SHARE_SELECT = """
    SELECT s.rightsholder_id, r.code AS rightsholder_code, r.name AS rightsholder_name,
           s.ratio
      FROM agreement_shares s
      JOIN rightsholders r ON r.id = s.rightsholder_id
"""


def _agreement_to_out(conn, agreement_id: int) -> dict:
    a = _fetch_one(
        conn,
        """
        SELECT a.*, w.code AS work_code, w.title AS work_title
          FROM agreements a JOIN works w ON w.id = a.work_id
         WHERE a.id = %s
        """,
        (agreement_id,),
    )
    shares = conn.execute(
        _SHARE_SELECT
        + " WHERE s.agreement_id = %s ORDER BY r.code",
        (agreement_id,),
    ).fetchall()
    return {
        "id": a["id"],
        "work_code": a["work_code"],
        "work_title": a["work_title"],
        "valid_from": a["valid_from"],
        "valid_to": a["valid_to"],
        "shares": [
            {
                "rightsholder_code": s["rightsholder_code"],
                "rightsholder_name": s["rightsholder_name"],
                "ratio": s["ratio"],
            }
            for s in shares
        ],
        "created_at": a["created_at"],
    }


def create_agreement(conn, body: AgreementIn) -> dict:
    work = _get_work_by_code(conn, body.work_code)

    if body.valid_to is not None and body.valid_to <= body.valid_from:
        raise conflict(
            "invalid_interval",
            "valid_to must be strictly after valid_from (interval is [from, to))",
        )

    codes = [s.rightsholder_code for s in body.shares]
    if len(set(codes)) != len(codes):
        raise conflict("duplicate_share", "rightsholder_code must not repeat")

    total = sum((s.ratio for s in body.shares), Decimal(0))
    if total != Decimal("100"):
        raise conflict(
            "ratio_total",
            "share ratios must sum to exactly 100",
            {"total": str(total)},
        )

    holders: dict[str, dict] = {}
    for code in codes:
        holders[code] = _get_rightsholder_by_code(conn, code)

    try:
        agreement_row = conn.execute(
            "INSERT INTO agreements (work_id, valid_from, valid_to)"
            " VALUES (%s, %s, %s) RETURNING id",
            (work["id"], body.valid_from, body.valid_to),
        ).fetchone()
    except psycopg.IntegrityError as exc:
        if getattr(exc, "sqlstate", "") in {"23P01", "23514"}:
            raise conflict(
                "agreement_overlap",
                f"agreement interval overlaps an existing agreement of work {body.work_code}",
            )
        raise
    agreement_id = agreement_row["id"]

    for s in body.shares:
        conn.execute(
            "INSERT INTO agreement_shares (agreement_id, rightsholder_id, ratio)"
            " VALUES (%s, %s, %s)",
            (agreement_id, holders[s.rightsholder_code]["id"], s.ratio),
        )
    return _agreement_to_out(conn, agreement_id)


def list_agreements(conn, work_code: Optional[str] = None) -> list[dict]:
    if work_code is not None:
        _get_work_by_code(conn, work_code)
        rows = conn.execute(
            """
            SELECT a.id FROM agreements a JOIN works w ON w.id = a.work_id
             WHERE w.code = %s ORDER BY a.valid_from
            """,
            (work_code,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM agreements ORDER BY valid_from").fetchall()
    return [_agreement_to_out(conn, r["id"]) for r in rows]


# ---------------------------------------------------------------------------
# Play records
# ---------------------------------------------------------------------------

# Chain-tail plays = effective records that take part in billing.
# ``outer`` is the alias of the play_records table in the outer query.
def _tail_condition(outer: str) -> str:
    return f"NOT EXISTS (SELECT 1 FROM play_records c WHERE c.corrects_id = {outer}.id)"


_MATCHED_PLAYS_SQL = """
    SELECT p.id AS play_id, p.code AS play_code, p.occurred_at, p.amount,
           w.id AS work_id, w.code AS work_code, w.title AS work_title,
           a.id AS agreement_id,
           s.rightsholder_id, r.code AS rightsholder_code,
           r.name AS rightsholder_name, s.ratio
      FROM play_records p
      JOIN works w ON w.id = p.work_id
      JOIN agreements a
        ON a.work_id = p.work_id
       AND a.valid_from <= p.occurred_at
       AND (a.valid_to IS NULL OR p.occurred_at < a.valid_to)
      JOIN agreement_shares s ON s.agreement_id = a.id
      JOIN rightsholders r ON r.id = s.rightsholder_id
     WHERE {tail}
       AND p.occurred_at >= %s
       AND p.occurred_at <  %s
     ORDER BY p.id, r.code
""".format(tail=_tail_condition("p"))

_PENDING_PLAYS_SQL = """
    SELECT p.id AS play_id, p.code, w.code AS work_code, w.title AS work_title,
           p.occurred_at, p.amount
      FROM play_records p
      JOIN works w ON w.id = p.work_id
     WHERE {tail}
       AND p.occurred_at >= %s
       AND p.occurred_at <  %s
       AND NOT EXISTS (
            SELECT 1 FROM agreements a
             WHERE a.work_id = p.work_id
               AND a.valid_from <= p.occurred_at
               AND (a.valid_to IS NULL OR p.occurred_at < a.valid_to)
       )
     ORDER BY p.occurred_at, p.id
""".format(tail=_tail_condition("p"))


def import_play(conn, body: PlayIn) -> tuple[dict, str, int]:
    """Returns (play_row, outcome, http_status).

    outcome is one of: created, idempotent, rejected.
    Raises ApiError on semantic rejection (404 / 409 / 422).
    """
    work = _get_work_by_code(conn, body.work_code)

    target_id: Optional[int] = None
    if body.corrects_code is not None:
        if body.corrects_code == body.code:
            raise conflict(
                "correction_cycle", "a play record cannot correct itself"
            )
        # Lock the target row so concurrent corrections cannot fork the chain.
        target = _fetch_one(
            conn,
            "SELECT * FROM play_records WHERE code = %s FOR UPDATE",
            (body.corrects_code,),
        )
        if target is None:
            raise not_found("corrected play", body.corrects_code)
        target_id = target["id"]

        child = _fetch_one(
            conn,
            "SELECT code FROM play_records WHERE corrects_id = %s",
            (target_id,),
        )
        if child is not None:
            raise conflict(
                "correction_fork",
                f"play {body.corrects_code} is already corrected by "
                f"{child['code']}; only the chain tail can be corrected",
            )

        # Defensive walk: structurally cycles cannot appear (a child is always
        # inserted after its parent and body.code is new), but guard explicitly.
        ancestor = target
        seen: set[int] = set()
        while ancestor is not None:
            if ancestor["id"] in seen:
                raise conflict(
                    "correction_cycle", "correction chain contains a cycle"
                )
            seen.add(ancestor["id"])
            ancestor = (
                _fetch_one(
                    conn,
                    "SELECT * FROM play_records WHERE id = %s",
                    (ancestor["corrects_id"],),
                )
                if ancestor["corrects_id"]
                else None
            )

    existing = _fetch_one(
        conn, "SELECT * FROM play_records WHERE code = %s", (body.code,)
    )
    if existing is not None:
        # Idempotent only when the whole content matches (correction target too).
        same_content = (
            existing["work_id"] == work["id"]
            and existing["occurred_at"] == body.occurred_at
            and existing["amount"] == body.amount
            and existing["corrects_id"] == target_id
        )
        if same_content:
            return _play_to_out(conn, existing), "idempotent", 200
        raise conflict(
            "play_code_content_mismatch",
            f"play code {body.code} already exists with different content",
        )

    try:
        row = conn.execute(
            """
            INSERT INTO play_records
                (code, work_id, occurred_at, amount, content_hash, corrects_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                body.code,
                work["id"],
                body.occurred_at,
                body.amount,
                _content_hash(work["id"], body.occurred_at, body.amount),
                target_id,
            ),
        ).fetchone()
    except psycopg.IntegrityError as exc:
        if _unique_violation(exc):
            raise conflict(
                "play_code_conflict", f"play code {body.code} already exists"
            )
        raise
    return _play_to_out(conn, row), "created", 201


def _play_to_out(conn, row: dict) -> dict:
    child = _fetch_one(
        conn, "SELECT id FROM play_records WHERE corrects_id = %s", (row["id"],)
    )
    work = _fetch_one(conn, "SELECT code, title FROM works WHERE id = %s", (row["work_id"],))
    corrects_code = None
    if row["corrects_id"] is not None:
        parent = _fetch_one(
            conn, "SELECT code FROM play_records WHERE id = %s", (row["corrects_id"],)
        )
        corrects_code = parent["code"] if parent else None
    return {
        "code": row["code"],
        "work_code": work["code"],
        "work_title": work["title"],
        "occurred_at": row["occurred_at"],
        "amount": row["amount"],
        "corrects_code": corrects_code,
        "superseded": child is not None,
        "is_chain_tail": child is None,
        "created_at": row["created_at"],
    }


def get_play_chain(conn, code: str) -> list[dict]:
    root = _fetch_one(conn, "SELECT * FROM play_records WHERE code = %s", (code,))
    if root is None:
        raise not_found("play", code)
    current_id = root["id"]
    while True:
        parent = _fetch_one(
            conn, "SELECT * FROM play_records WHERE id = %s", (current_id,)
        )
        if parent["corrects_id"] is None:
            root = parent
            break
        current_id = parent["corrects_id"]
    rows: list[dict] = []
    cur = root
    while cur is not None:
        rows.append(_play_to_out(conn, cur))
        child = _fetch_one(
            conn, "SELECT * FROM play_records WHERE corrects_id = %s ORDER BY id",
            (cur["id"],),
        )
        cur = child
    return rows


def list_plays(conn, include_superseded: bool = False) -> list[dict]:
    sql = "SELECT * FROM play_records"
    if not include_superseded:
        sql = "SELECT * FROM play_records p WHERE " + _tail_condition("p")
    sql += " ORDER BY occurred_at, id"
    return [_play_to_out(conn, r) for r in conn.execute(sql).fetchall()]


# ---------------------------------------------------------------------------
# Settlement computation
# ---------------------------------------------------------------------------


def _load_prior_documents(conn, period_id: int) -> list[dict]:
    return conn.execute(
        """
        SELECT st.id, st.period_id, st.seq, st.kind, st.doc_no, st.issued_at,
               p.period_from, p.period_to,
               (SELECT COUNT(*) FROM statement_items WHERE statement_id = st.id) AS item_count,
               (SELECT COALESCE(SUM(amount), 0) FROM statement_items WHERE statement_id = st.id) AS total_amount
          FROM statements st
          JOIN settlement_periods p ON p.id = st.period_id
         WHERE st.period_id = %s
         ORDER BY st.seq
        """,
        (period_id,),
    ).fetchall()


def _prior_totals(conn, period_id: int) -> dict[int, dict]:
    """Sum of amounts across all already-issued documents of this period."""
    rows = conn.execute(
        """
        SELECT i.rightsholder_id, i.rightsholder_code, r.name AS rightsholder_name,
               SUM(i.amount) AS amount
          FROM statement_items i
          JOIN statements st ON st.id = i.statement_id
          JOIN rightsholders r ON r.id = i.rightsholder_id
         WHERE st.period_id = %s
         GROUP BY i.rightsholder_id, i.rightsholder_code, r.name
        """,
        (period_id,),
    ).fetchall()
    return {
        r["rightsholder_id"]: {
            "rightsholder_code": r["rightsholder_code"],
            "rightsholder_name": r["rightsholder_name"],
            "amount": int(r["amount"]),
        }
        for r in rows
    }


def compute_period(
    conn, period_from: datetime, period_to: datetime, with_allocations: bool = False
) -> dict:
    if period_to <= period_from:
        raise conflict(
            "invalid_period",
            "period_to must be strictly after period_from (period is [from, to))",
        )

    rows = conn.execute(_MATCHED_PLAYS_SQL, (period_from, period_to)).fetchall()

    # Group shares by play so each play is split independently.
    plays: dict[int, dict] = {}
    for r in rows:
        play = plays.setdefault(
            r["play_id"],
            {
                "play_code": r["play_code"],
                "work_code": r["work_code"],
                "occurred_at": r["occurred_at"],
                "amount": int(r["amount"]),
                "shares": [],
                "names": {},
            },
        )
        play["shares"].append(
            Share(r["rightsholder_code"], Decimal(r["ratio"]))
        )
        play["names"][r["rightsholder_code"]] = r["rightsholder_name"]

    payable: dict[str, dict] = {}
    allocation_lines: list[dict] = []

    for play in plays.values():
        allocs = split_amount(play["amount"], play["shares"])
        # Conservation is guaranteed by split_amount; assert as an extra guard.
        assert sum(a.amount for a in allocs) == play["amount"]
        line_items = []
        for a in allocs:
            holder_name = play["names"][a.rightsholder_code]
            entry = payable.setdefault(
                a.rightsholder_code,
                {
                    "rightsholder_code": a.rightsholder_code,
                    "rightsholder_name": holder_name,
                    "amount": 0,
                },
            )
            entry["amount"] += a.amount
            line_items.append(
                {
                    "rightsholder_code": a.rightsholder_code,
                    "rightsholder_name": holder_name,
                    "amount": a.amount,
                }
            )
        if with_allocations:
            allocation_lines.append(
                {
                    "play_code": play["play_code"],
                    "work_code": play["work_code"],
                    "occurred_at": play["occurred_at"],
                    "amount": play["amount"],
                    "allocation": line_items,
                }
            )

    payable_list = sorted(payable.values(), key=lambda x: x["rightsholder_code"])

    pending_rows = conn.execute(
        _PENDING_PLAYS_SQL, (period_from, period_to)
    ).fetchall()
    pending = [
        {
            "code": r["code"],
            "work_code": r["work_code"],
            "work_title": r["work_title"],
            "occurred_at": r["occurred_at"],
            "amount": int(r["amount"]),
        }
        for r in pending_rows
    ]

    period = _fetch_one(
        conn,
        "SELECT * FROM settlement_periods WHERE period_from = %s AND period_to = %s",
        (period_from, period_to),
    )
    prior_docs: list[dict] = []
    prior_total_list: list[dict] = []
    if period is not None:
        prior_docs = [
            _statement_summary(r) for r in _load_prior_documents(conn, period["id"])
        ]
        prior_total_list = sorted(
            _prior_totals(conn, period["id"]).values(),
            key=lambda x: x["rightsholder_code"],
        )

    result: dict[str, Any] = {
        "period_from": period_from,
        "period_to": period_to,
        "payable": payable_list,
        "pending": pending,
        "prior_documents": prior_docs,
        "prior_total": prior_total_list,
    }
    if with_allocations:
        result["allocations"] = allocation_lines
    return result


def _statement_summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "doc_no": row["doc_no"],
        "kind": row["kind"],
        "seq": row["seq"],
        "period_from": row["period_from"],
        "period_to": row["period_to"],
        "issued_at": row["issued_at"],
        "item_count": row["item_count"],
        "total_amount": int(row["total_amount"]),
    }


def _latest_pending(conn, period_id: int) -> list[dict]:
    latest = _fetch_one(
        conn,
        "SELECT id FROM statements WHERE period_id = %s ORDER BY seq DESC LIMIT 1",
        (period_id,),
    )
    if latest is None:
        return []
    return conn.execute(
        """
        SELECT code, work_code, work_title, occurred_at, amount
          FROM statement_pending
         WHERE statement_id = %s
         ORDER BY occurred_at, play_id
        """,
        (latest["id"],),
    ).fetchall()


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def issue_statement(conn, body: PeriodIn) -> dict:
    if body.period_to <= body.period_from:
        raise conflict(
            "invalid_period",
            "period_to must be strictly after period_from (period is [from, to))",
        )

    # Serialise issuances for the same period via a transaction-scoped advisory
    # lock derived from the period boundaries. Different periods use different
    # lock keys and never block each other.
    period_row = conn.execute(
        """
        SELECT pg_advisory_xact_lock(
            hashtextextended(
                COALESCE(%s::text, '') || '/' || COALESCE(%s::text, ''), 0
            )
        )
        """,
        (body.period_from.isoformat(), body.period_to.isoformat()),
    ).fetchone()

    period_row = conn.execute(
        """
        INSERT INTO settlement_periods (period_from, period_to)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (body.period_from, body.period_to),
    ).fetchone()
    if period_row is None:
        period_row = _fetch_one(
            conn,
            "SELECT id FROM settlement_periods WHERE period_from = %s AND period_to = %s",
            (body.period_from, body.period_to),
        )
    period_id = period_row["id"]

    prior_docs = _load_prior_documents(conn, period_id)
    seq = len(prior_docs)
    kind = "initial" if seq == 0 else "adjustment"

    current = compute_period(conn, body.period_from, body.period_to)
    current_pay = {x["rightsholder_code"]: x for x in current["payable"]}
    prior_pay = {
        x["rightsholder_code"]: x
        for x in _prior_totals(conn, period_id).values()
    }

    items: list[dict] = []
    all_codes = sorted(set(current_pay) | set(prior_pay))
    for code in all_codes:
        new_amount = int(current_pay.get(code, {}).get("amount", 0))
        old_amount = int(prior_pay.get(code, {}).get("amount", 0))
        delta = new_amount - old_amount
        if kind == "initial" or delta != 0:
            name = (
                current_pay.get(code, {}).get("rightsholder_name")
                or prior_pay.get(code, {}).get("rightsholder_name")
                or code
            )
            items.append(
                {
                    "rightsholder_code": code,
                    "rightsholder_name": name,
                    "amount": new_amount if kind == "initial" else delta,
                    "new_amount": new_amount,
                }
            )

    pending = current["pending"]
    prior_pending = _latest_pending(conn, period_id)
    pending_changed = {p["code"] for p in pending} != {
        p["code"] for p in prior_pending
    }

    if kind == "initial" and not items and not pending:
        raise ApiError(
            409,
            "empty_period",
            "no effective play records found in this period; nothing to issue",
        )
    if kind == "adjustment" and not items and not pending_changed:
        raise ApiError(
            409,
            "no_change",
            "no new agreement or play correction changes this period; "
            "duplicate adjustment statement is not allowed",
        )

    doc_no = _make_doc_no(period_id, body.period_from, seq)
    stmt_row = conn.execute(
        """
        INSERT INTO statements (period_id, seq, kind, doc_no)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (period_id, seq) DO NOTHING
        RETURNING id
        """,
        (period_id, seq, kind, doc_no),
    ).fetchone()
    if stmt_row is None:
        raise ApiError(
            409,
            "concurrent_statement",
            "a statement for this sequence was issued concurrently; retry",
        )
    statement_id = stmt_row["id"]

    for it in items:
        holder = _fetch_one(
            conn, "SELECT id FROM rightsholders WHERE code = %s", (it["rightsholder_code"],)
        )
        conn.execute(
            "INSERT INTO statement_items (statement_id, rightsholder_id, rightsholder_code, amount)"
            " VALUES (%s, %s, %s, %s)",
            (statement_id, holder["id"], it["rightsholder_code"], it["amount"]),
        )

    for p in pending:
        play = _fetch_one(conn, "SELECT id FROM play_records WHERE code = %s", (p["code"],))
        conn.execute(
            """
            INSERT INTO statement_pending
                (statement_id, play_id, code, work_code, work_title, occurred_at, amount)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                statement_id,
                play["id"],
                p["code"],
                p["work_code"],
                p["work_title"],
                p["occurred_at"],
                p["amount"],
            ),
        )

    return get_statement_by_id(conn, statement_id)


def _make_doc_no(period_id: int, period_from: datetime, seq: int) -> str:
    return f"{settings.doc_prefix}-{period_from.strftime('%Y%m')}-P{period_id}-{seq}"


# ---------------------------------------------------------------------------
# Statement reads
# ---------------------------------------------------------------------------


def _period_row(conn, period_id: int) -> dict:
    return _fetch_one(
        conn, "SELECT * FROM settlement_periods WHERE id = %s", (period_id,)
    )


def _statement_full(conn, row: dict) -> dict:
    period = _period_row(conn, row["period_id"])
    items = conn.execute(
        """
        SELECT i.rightsholder_code, r.name AS rightsholder_name, i.amount
          FROM statement_items i
          JOIN rightsholders r ON r.id = i.rightsholder_id
         WHERE i.statement_id = %s
         ORDER BY i.rightsholder_code
        """,
        (row["id"],),
    ).fetchall()
    pending = conn.execute(
        """
        SELECT code, work_code, work_title, occurred_at, amount
          FROM statement_pending
         WHERE statement_id = %s
         ORDER BY occurred_at, play_id
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "doc_no": row["doc_no"],
        "kind": row["kind"],
        "seq": row["seq"],
        "period_from": period["period_from"],
        "period_to": period["period_to"],
        "issued_at": row["issued_at"],
        "items": [
            {
                "rightsholder_code": x["rightsholder_code"],
                "rightsholder_name": x["rightsholder_name"],
                "amount": int(x["amount"]),
            }
            for x in items
        ],
        "pending": [
            {
                "code": x["code"],
                "work_code": x["work_code"],
                "work_title": x["work_title"],
                "occurred_at": x["occurred_at"],
                "amount": int(x["amount"]),
            }
            for x in pending
        ],
    }


def get_statement_by_id(conn, statement_id: int) -> dict:
    row = _fetch_one(conn, "SELECT * FROM statements WHERE id = %s", (statement_id,))
    if row is None:
        raise not_found("statement", str(statement_id))
    return _statement_full(conn, row)


def list_statements(
    conn, period_from: Optional[datetime], period_to: Optional[datetime]
) -> list[dict]:
    sql = """
        SELECT st.*, p.period_from AS p_from, p.period_to AS p_to,
               (SELECT COUNT(*) FROM statement_items WHERE statement_id = st.id) AS item_count,
               (SELECT COALESCE(SUM(amount), 0) FROM statement_items WHERE statement_id = st.id) AS total_amount
          FROM statements st
          JOIN settlement_periods p ON p.id = st.period_id
    """
    params: list[Any] = []
    where = []
    if period_from is not None:
        where.append("p.period_from = %s")
        params.append(period_from)
    if period_to is not None:
        where.append("p.period_to = %s")
        params.append(period_to)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.period_from, st.seq"
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        {
            "id": r["id"],
            "doc_no": r["doc_no"],
            "kind": r["kind"],
            "seq": r["seq"],
            "period_from": r["p_from"],
            "period_to": r["p_to"],
            "issued_at": r["issued_at"],
            "item_count": r["item_count"],
            "total_amount": int(r["total_amount"]),
        }
        for r in rows
    ]
