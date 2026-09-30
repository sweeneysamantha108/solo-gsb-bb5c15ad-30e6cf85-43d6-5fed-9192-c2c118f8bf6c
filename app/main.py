from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Query, Response
from psycopg import Connection

from . import db, service
from .errors import ApiError, api_error_handler
from .models import (
    AgreementIn,
    AgreementOut,
    PeriodComputationOut,
    PeriodIn,
    PlayBatchIn,
    PlayBatchOut,
    PlayIn,
    PlayOut,
    RightsholderIn,
    RightsholderOut,
    StatementOut,
    StatementSummaryOut,
    WorkIn,
    WorkOut,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_pool()
    db.init_schema()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(
    title="演出录音播放版税结算服务",
    version="1.0.0",
    description=(
        "登记作品与分成协议、导入播放记录、按期间出具权利人应付单与追补单。\n\n"
        "- 金额均为非负整数，单位为最小货币单位（如分）。\n"
        "- 协议区间为半开区间 [valid_from, valid_to)。\n"
    ),
    lifespan=lifespan,
)
app.add_exception_handler(ApiError, api_error_handler)


def get_conn() -> Connection:
    with db.conn() as connection:
        yield connection


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", tags=["system"])
def health(conn: Connection = Depends(get_conn)):
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Rightsholders
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/rightsholders",
    response_model=RightsholderOut,
    status_code=201,
    tags=["rightsholders"],
)
def create_rightsholder(
    body: RightsholderIn,
    response: Response,
    conn: Connection = Depends(get_conn),
):
    row, created = service.create_rightsholder(conn, body)
    conn.commit()
    response.status_code = 201 if created else 200
    return row


@app.get(
    "/api/v1/rightsholders",
    response_model=list[RightsholderOut],
    tags=["rightsholders"],
)
def list_rightsholders(conn: Connection = Depends(get_conn)):
    return service.list_rightsholders(conn)


# ---------------------------------------------------------------------------
# Works
# ---------------------------------------------------------------------------


@app.post("/api/v1/works", response_model=WorkOut, status_code=201, tags=["works"])
def create_work(body: WorkIn, response: Response, conn: Connection = Depends(get_conn)):
    row, created = service.create_work(conn, body)
    conn.commit()
    response.status_code = 201 if created else 200
    return row


@app.get("/api/v1/works", response_model=list[WorkOut], tags=["works"])
def list_works(conn: Connection = Depends(get_conn)):
    return service.list_works(conn)


# ---------------------------------------------------------------------------
# Agreements
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/agreements",
    response_model=AgreementOut,
    status_code=201,
    tags=["agreements"],
)
def create_agreement(body: AgreementIn, conn: Connection = Depends(get_conn)):
    row = service.create_agreement(conn, body)
    conn.commit()
    return row


@app.get(
    "/api/v1/agreements",
    response_model=list[AgreementOut],
    tags=["agreements"],
)
def list_agreements(
    work_code: Optional[str] = Query(default=None),
    conn: Connection = Depends(get_conn),
):
    return service.list_agreements(conn, work_code)


# ---------------------------------------------------------------------------
# Plays
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/plays",
    response_model=PlayOut,
    status_code=201,
    tags=["plays"],
    responses={
        200: {"description": "同编号同内容重复导入（幂等）"},
        201: {"description": "已登记"},
        404: {"description": "作品 / 被更正记录不存在"},
        422: {"description": "同编号异内容、分叉或循环更正等"},
    },
)
def import_play(body: PlayIn, response: Response, conn: Connection = Depends(get_conn)):
    row, _outcome, status = service.import_play(conn, body)
    conn.commit()
    response.status_code = status
    return row


@app.post(
    "/api/v1/plays/batch",
    response_model=PlayBatchOut,
    tags=["plays"],
)
def import_plays_batch(body: PlayBatchIn, conn: Connection = Depends(get_conn)):
    items = []
    counts = {"created": 0, "idempotent": 0, "rejected": 0}
    for play in body.plays:
        try:
            with conn.transaction():  # savepoint per item
                _row, outcome, status = service.import_play(conn, play)
            items.append(
                {
                    "code": play.code,
                    "status": outcome,
                    "http_status": status,
                    "error": None,
                    "error_code": None,
                }
            )
            counts[outcome] += 1
        except ApiError as exc:
            # The savepoint created by conn.transaction() was rolled back
            # automatically when the exception propagated.
            items.append(
                {
                    "code": play.code,
                    "status": "rejected",
                    "http_status": exc.status_code,
                    "error": exc.message,
                    "error_code": exc.code,
                }
            )
            counts["rejected"] += 1
    conn.commit()
    return {**counts, "items": items}


@app.get("/api/v1/plays", response_model=list[PlayOut], tags=["plays"])
def list_plays(
    include_superseded: bool = Query(default=False),
    conn: Connection = Depends(get_conn),
):
    return service.list_plays(conn, include_superseded)


@app.get("/api/v1/plays/{code}/chain", response_model=list[PlayOut], tags=["plays"])
def get_play_chain(code: str, conn: Connection = Depends(get_conn)):
    return service.get_play_chain(conn, code)


# ---------------------------------------------------------------------------
# Settlement: dry run and issuance
# ---------------------------------------------------------------------------


@app.get(
    "/api/v1/settlement/compute",
    response_model=PeriodComputationOut,
    tags=["settlement"],
)
def compute_period(
    period_from: datetime = Query(..., description="ISO-8601，含"),
    period_to: datetime = Query(..., description="ISO-8601，不含"),
    detail: bool = Query(default=False, description="返回逐笔分账明细"),
    conn: Connection = Depends(get_conn),
):
    return service.compute_period(conn, _as_aware(period_from), _as_aware(period_to), detail)


@app.post(
    "/api/v1/settlement/statements",
    response_model=StatementOut,
    status_code=201,
    tags=["settlement"],
    responses={
        409: {"description": "期间无记录 / 重复出具（无变化追补）"},
    },
)
def issue_statement(body: PeriodIn, conn: Connection = Depends(get_conn)):
    row = service.issue_statement(conn, body)
    conn.commit()
    return row


@app.get(
    "/api/v1/settlement/statements",
    response_model=list[StatementSummaryOut],
    tags=["settlement"],
)
def list_statements(
    period_from: Optional[datetime] = Query(default=None),
    period_to: Optional[datetime] = Query(default=None),
    conn: Connection = Depends(get_conn),
):
    return service.list_statements(
        conn, _as_aware(period_from), _as_aware(period_to)
    )


@app.get(
    "/api/v1/settlement/statements/{statement_id}",
    response_model=StatementOut,
    tags=["settlement"],
)
def get_statement(statement_id: int, conn: Connection = Depends(get_conn)):
    return service.get_statement_by_id(conn, statement_id)
