from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.functional_validators import BeforeValidator
from typing_extensions import Annotated


# Accept ISO-8601 strings; always normalise to timezone-aware datetimes.
def _as_aware_dt(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        # Bare timestamps are interpreted as UTC rather than rejected.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


AwareDatetime = Annotated[datetime, BeforeValidator(_as_aware_dt)]

# Ratios are money-like: build Decimals from strings, never binary floats.
DecimalIn = Annotated[
    Decimal,
    BeforeValidator(lambda v: Decimal(str(v)) if v is not None else v),
]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- Rightsholders / works ---------------------------------------------------


class RightsholderIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)


class RightsholderOut(ORMModel):
    id: int
    code: str
    name: str
    created_at: datetime


class WorkIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)


class WorkOut(ORMModel):
    id: int
    code: str
    title: str
    created_at: datetime


# ---- Agreements --------------------------------------------------------------


class ShareIn(BaseModel):
    rightsholder_code: str = Field(min_length=1, max_length=64)
    ratio: DecimalIn = Field(gt=0)


class AgreementIn(BaseModel):
    work_code: str = Field(min_length=1, max_length=64)
    valid_from: AwareDatetime
    valid_to: Optional[AwareDatetime] = None
    shares: list[ShareIn] = Field(min_length=1)


class ShareOut(BaseModel):
    rightsholder_code: str
    rightsholder_name: str
    ratio: Decimal

    @field_serializer("ratio")
    def _ratio_to_str(self, value: Decimal) -> str:
        # Financial values are emitted as strings so clients never lose
        # precision through binary-float JSON numbers.
        return format(value, "f")


class AgreementOut(BaseModel):
    id: int
    work_code: str
    work_title: str
    valid_from: datetime
    valid_to: Optional[datetime]
    shares: list[ShareOut]
    created_at: datetime


# ---- Plays -------------------------------------------------------------------


class PlayIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    work_code: str = Field(min_length=1, max_length=64)
    occurred_at: AwareDatetime
    amount: int = Field(ge=0)
    corrects_code: Optional[str] = Field(default=None, max_length=64)


class PlayBatchIn(BaseModel):
    plays: list[PlayIn] = Field(min_length=1)


class PlayOut(ORMModel):
    code: str
    work_code: str
    work_title: str
    occurred_at: datetime
    amount: int
    corrects_code: Optional[str]
    superseded: bool
    is_chain_tail: bool
    created_at: datetime


class PlayBatchItemOut(BaseModel):
    code: str
    status: Literal["created", "idempotent", "rejected"]
    http_status: int
    error: Optional[str] = None
    error_code: Optional[str] = None


class PlayBatchOut(BaseModel):
    created: int
    idempotent: int
    rejected: int
    items: list[PlayBatchItemOut]


# ---- Settlement --------------------------------------------------------------


class PeriodIn(BaseModel):
    period_from: AwareDatetime
    period_to: AwareDatetime


class StatementItemOut(BaseModel):
    rightsholder_code: str
    rightsholder_name: str
    amount: int


class PendingPlayOut(BaseModel):
    code: str
    work_code: str
    work_title: str
    occurred_at: datetime
    amount: int


class StatementOut(BaseModel):
    id: int
    doc_no: str
    kind: Literal["initial", "adjustment"]
    seq: int
    period_from: datetime
    period_to: datetime
    issued_at: datetime
    items: list[StatementItemOut]
    pending: list[PendingPlayOut]


class StatementSummaryOut(BaseModel):
    id: int
    doc_no: str
    kind: Literal["initial", "adjustment"]
    seq: int
    period_from: datetime
    period_to: datetime
    issued_at: datetime
    item_count: int
    total_amount: int


class AllocationLineOut(BaseModel):
    play_code: str
    work_code: str
    occurred_at: datetime
    amount: int
    allocation: list[StatementItemOut]


class PeriodComputationOut(BaseModel):
    period_from: datetime
    period_to: datetime
    payable: list[StatementItemOut]
    pending: list[PendingPlayOut]
    prior_documents: list[StatementSummaryOut]
    prior_total: list[StatementItemOut]
    allocations: Optional[list[AllocationLineOut]] = None
