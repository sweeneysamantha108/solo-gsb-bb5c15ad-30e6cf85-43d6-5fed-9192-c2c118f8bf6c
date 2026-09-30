"""End-to-end integration test against a real PostgreSQL.

Designed to run against the pgserver launched in CI/sandbox:
    DATABASE_URL=postgresql://postgres:@/postgres?host=/tmp/pgdata \
        python tests/integration_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Optional: launch a portable PostgreSQL (pip install pgserver) when no
# external DATABASE_URL is provided. Used by the local test harness.
if not os.environ.get("DATABASE_URL"):
    import pgserver

    _pg = pgserver.get_server("/tmp/pgdata-it")
    os.environ["DATABASE_URL"] = _pg.get_uri()
    # Portable build lacks btree_gist; use equivalent trigger-based exclusion.
    os.environ["ROYALTY_TEST_FALLBACK_INDEX"] = "1"

from app import db  # noqa: E402

db.init_pool()

# The portable pgserver build does not package btree_gist (the official
# postgres Docker image does). For the local test run only, apply the schema
# with the GiST exclusion index replaced by an equivalent exclusion trigger.
if os.environ.get("ROYALTY_TEST_FALLBACK_INDEX") == "1":
    from app.config import settings

    ddl = Path(settings.schema_path).read_text(encoding="utf-8")
    ddl = ddl.replace(
        "CREATE EXTENSION IF NOT EXISTS btree_gist;",
        "-- btree_gist unavailable in portable test build",
    )
    import re

    ddl = re.sub(
        r"CREATE UNIQUE INDEX IF NOT EXISTS uq_agreements_work_interval.*?\);",
        """
        CREATE OR REPLACE FUNCTION assert_no_agreement_overlap()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM agreements a
                 WHERE a.work_id = NEW.work_id AND a.id <> NEW.id
                   AND a.valid_from < COALESCE(NEW.valid_to, 'infinity'::timestamptz)
                   AND COALESCE(a.valid_to, 'infinity'::timestamptz) > NEW.valid_from
            ) THEN
                RAISE EXCEPTION 'agreement interval overlaps' USING ERRCODE = '23P01';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER trg_no_overlap BEFORE INSERT OR UPDATE ON agreements
            FOR EACH ROW EXECUTE FUNCTION assert_no_agreement_overlap();
        """,
        ddl,
        flags=re.S,
    )
    with db.get_pool().connection() as _c:
        _c.execute(ddl)
        _c.commit()
else:
    db.init_schema()


from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

# Schema was applied (or fallback-applied) above; the lifespan must not try to
# re-run the portable-build-incompatible extension statement.
db.init_schema = lambda: None

# Bypass lifespan pool management: already initialised above.
client = TestClient(app)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def expect_status(name: str, resp, expected: int):
    check(name, resp.status_code == expected,
          f"want {expected} got {resp.status_code}: {resp.text}")
    return resp.json() if resp.content else {}


# ---------------------------------------------------------------------------

def run():
    with client:
        expect_status("health", client.get("/health"), 200)

        # ---- rightsholders / works ----
        for code, nm in [("RH-001", "甲"), ("RH-002", "乙"), ("RH-003", "丙")]:
            expect_status(
                f"create rightsholder {code}",
                client.post("/api/v1/rightsholders", json={"code": code, "name": nm}),
                201,
            )
        # idempotent same name -> 200
        expect_status(
            "rightsholder idempotent 200",
            client.post("/api/v1/rightsholders", json={"code": "RH-001", "name": "甲"}),
            200,
        )
        # different name -> 422
        expect_status(
            "rightsholder same code diff name 422",
            client.post("/api/v1/rightsholders", json={"code": "RH-001", "name": "其他"}),
            422,
        )

        for code, title in [("W-001", "作品A"), ("W-002", "作品B")]:
            expect_status(
                f"create work {code}",
                client.post("/api/v1/works", json={"code": code, "title": title}),
                201,
            )

        # ---- agreements ----
        ok = expect_status(
            "agreement 50/30/20",
            client.post(
                "/api/v1/agreements",
                json={
                    "work_code": "W-001",
                    "valid_from": "2026-01-01T00:00:00Z",
                    "valid_to": "2026-07-01T00:00:00Z",
                    "shares": [
                        {"rightsholder_code": "RH-001", "ratio": "50"},
                        {"rightsholder_code": "RH-002", "ratio": "30"},
                        {"rightsholder_code": "RH-003", "ratio": "20"},
                    ],
                },
            ),
            201,
        )
        check("ratio serialized as string", isinstance(ok["shares"][0]["ratio"], str))

        expect_status(
            "overlap rejected",
            client.post(
                "/api/v1/agreements",
                json={
                    "work_code": "W-001",
                    "valid_from": "2026-03-01T00:00:00Z",
                    "valid_to": "2026-08-01T00:00:00Z",
                    "shares": [{"rightsholder_code": "RH-001", "ratio": "100"}],
                },
            ),
            422,
        )

        # adjacent intervals do NOT overlap: one ends exactly when next starts
        expect_status(
            "adjacent agreement allowed",
            client.post(
                "/api/v1/agreements",
                json={
                    "work_code": "W-001",
                    "valid_from": "2026-07-01T00:00:00Z",
                    "valid_to": None,
                    "shares": [
                        {"rightsholder_code": "RH-001", "ratio": "70"},
                        {"rightsholder_code": "RH-002", "ratio": "30"},
                    ],
                },
            ),
            201,
        )

        expect_status(
            "ratio total 90 rejected",
            client.post(
                "/api/v1/agreements",
                json={
                    "work_code": "W-002",
                    "valid_from": "2026-01-01T00:00:00Z",
                    "shares": [
                        {"rightsholder_code": "RH-001", "ratio": "60"},
                        {"rightsholder_code": "RH-002", "ratio": "30"},
                    ],
                },
            ),
            422,
        )

        expect_status(
            "duplicate share rejected",
            client.post(
                "/api/v1/agreements",
                json={
                    "work_code": "W-002",
                    "valid_from": "2026-01-01T00:00:00Z",
                    "shares": [
                        {"rightsholder_code": "RH-001", "ratio": "50"},
                        {"rightsholder_code": "RH-001", "ratio": "50"},
                    ],
                },
            ),
            422,
        )

        # ---- plays: idempotency and corrections ----
        expect_status(
            "import P-0001 100",
            client.post("/api/v1/plays", json={
                "code": "P-0001", "work_code": "W-001",
                "occurred_at": "2026-02-10T20:00:00Z", "amount": 100}),
            201,
        )
        expect_status(
            "P-0001 idempotent",
            client.post("/api/v1/plays", json={
                "code": "P-0001", "work_code": "W-001",
                "occurred_at": "2026-02-10T20:00:00Z", "amount": 100}),
            200,
        )
        expect_status(
            "P-0001 different content rejected",
            client.post("/api/v1/plays", json={
                "code": "P-0001", "work_code": "W-001",
                "occurred_at": "2026-02-10T20:00:00Z", "amount": 199}),
            422,
        )

        # 101 split: floor 50.5/30.3/20.2 -> 50/30/20, leftover 1 to RH-001
        expect_status(
            "import P-0002 101",
            client.post("/api/v1/plays", json={
                "code": "P-0002", "work_code": "W-001",
                "occurred_at": "2026-02-11T20:00:00Z", "amount": 101}),
            201,
        )

        # play for work with no agreement -> pending later
        expect_status(
            "import P-0003 W-002 no agreement",
            client.post("/api/v1/plays", json={
                "code": "P-0003", "work_code": "W-002",
                "occurred_at": "2026-02-12T20:00:00Z", "amount": 250}),
            201,
        )

        # correction: P-0001 -> P-0001-V2 amount 90
        expect_status(
            "correction P-0001-V2",
            client.post("/api/v1/plays", json={
                "code": "P-0001-V2", "work_code": "W-001",
                "occurred_at": "2026-02-10T20:00:00Z", "amount": 90,
                "corrects_code": "P-0001"}),
            201,
        )
        # fork rejected
        r = expect_status(
            "fork rejected",
            client.post("/api/v1/plays", json={
                "code": "P-0001-ALT", "work_code": "W-001",
                "occurred_at": "2026-02-10T20:00:00Z", "amount": 80,
                "corrects_code": "P-0001"}),
            422,
        )
        check("fork error code", r["error"]["code"] == "correction_fork", r)
        # self cycle rejected
        expect_status(
            "self correction rejected",
            client.post("/api/v1/plays", json={
                "code": "P-X", "work_code": "W-001",
                "occurred_at": "2026-02-10T20:00:00Z", "amount": 1,
                "corrects_code": "P-X"}),
            422,
        )
        # missing correction target
        expect_status(
            "missing correction target 404",
            client.post("/api/v1/plays", json={
                "code": "P-Y", "work_code": "W-001",
                "occurred_at": "2026-02-10T20:00:00Z", "amount": 1,
                "corrects_code": "NOPE"}),
            404,
        )

        chain = expect_status(
            "chain query",
            client.get("/api/v1/plays/P-0001/chain"),
            200,
        )
        check("chain has 2 rows", len(chain) == 2, chain)
        check("chain root superseded", chain[0]["code"] == "P-0001" and chain[0]["superseded"])
        check("chain tail active", chain[1]["code"] == "P-0001-V2" and chain[1]["is_chain_tail"])

        tails = client.get("/api/v1/plays").json()
        tail_codes = {p["code"] for p in tails}
        check("P-0001 excluded from tails", "P-0001" not in tail_codes)
        check("P-0001-V2 in tails", "P-0001-V2" in tail_codes)

        # batch import
        b = expect_status(
            "batch import",
            client.post("/api/v1/plays/batch", json={"plays": [
                {"code": "P-0010", "work_code": "W-001",
                 "occurred_at": "2026-03-01T00:00:00Z", "amount": 3},
                {"code": "P-0010", "work_code": "W-001",
                 "occurred_at": "2026-03-01T00:00:00Z", "amount": 3},
                {"code": "P-0010", "work_code": "W-001",
                 "occurred_at": "2026-03-01T00:00:00Z", "amount": 9},
                {"code": "P-0011", "work_code": "GHOST",
                 "occurred_at": "2026-03-01T00:00:00Z", "amount": 3},
            ]}),
            200,
        )
        check("batch counts", b["created"] == 1 and b["idempotent"] == 1 and b["rejected"] == 2, b)

        # ---- compute ----
        comp = expect_status(
            "compute Q1 with detail",
            client.get(
                "/api/v1/settlement/compute"
                "?period_from=2026-01-01T00:00:00Z&period_to=2026-04-01T00:00:00Z&detail=true"
            ),
            200,
        )
        # Effective plays in Q1 for W-001:
        #   P-0001-V2 90  -> 45/27/18
        #   P-0002 101     -> floor 50.5/30.3/20.2 = 50/30/20; one leftover to
        #                    largest fraction RH-001(.5, tie by code) -> 51/30/20
        #   P-0010 3       -> floor 1.5/0.9/0.6 = 1/0/0; two leftovers go to the
        #                    two largest fractions RH-002(.9), RH-003(.6) -> 1/1/1
        # totals RH-001:45+51+1=97, RH-002:27+30+1=58, RH-003:18+20+1=39 = 194
        pay = {x["rightsholder_code"]: x["amount"] for x in comp["payable"]}
        check("payable totals", pay == {"RH-001": 97, "RH-002": 58, "RH-003": 39}, pay)
        check("sum payable = effective plays", sum(pay.values()) == 194, pay)
        pending_codes = {p["code"] for p in comp["pending"]}
        check("P-0003 pending", pending_codes == {"P-0003"}, comp["pending"])
        # per-play conservation in detail
        for line in comp["allocations"]:
            check(
                f"conservation {line['play_code']}",
                sum(a["amount"] for a in line["allocation"]) == line["amount"],
                line,
            )
        check("no prior documents yet", comp["prior_documents"] == [] and comp["prior_total"] == [])

        # play in July uses second agreement (70/30), no RH-003
        client.post("/api/v1/plays", json={
            "code": "P-JUL", "work_code": "W-001",
            "occurred_at": "2026-07-02T00:00:00Z", "amount": 100})
        comp2 = client.get(
            "/api/v1/settlement/compute"
            "?period_from=2026-07-01T00:00:00Z&period_to=2026-08-01T00:00:00Z"
        ).json()
        pay2 = {x["rightsholder_code"]: x["amount"] for x in comp2["payable"]}
        check("july uses new agreement 70/30", pay2 == {"RH-001": 70, "RH-002": 30}, pay2)

        # ---- issue initial statement ----
        period = {"period_from": "2026-01-01T00:00:00Z",
                  "period_to": "2026-04-01T00:00:00Z"}
        stmt1 = expect_status("initial statement", client.post(
            "/api/v1/settlement/statements", json=period), 201)
        check("stmt kind initial seq0", stmt1["kind"] == "initial" and stmt1["seq"] == 0)
        items1 = {i["rightsholder_code"]: i["amount"] for i in stmt1["items"]}
        check("stmt1 amounts", items1 == {"RH-001": 97, "RH-002": 58, "RH-003": 39}, items1)
        check("stmt1 pending snapshot",
              {p["code"] for p in stmt1["pending"]} == {"P-0003"})

        # document immutability: re-issue identical period -> 409 no_change
        expect_status("duplicate issue 409", client.post(
            "/api/v1/settlement/statements", json=period), 409)

        # empty period -> 409 empty
        expect_status("empty period 409", client.post(
            "/api/v1/settlement/statements", json={
                "period_from": "2030-01-01T00:00:00Z",
                "period_to": "2030-02-01T00:00:00Z"}), 409)

        # ---- late agreement for W-002, then adjustment ----
        expect_status("late W-002 agreement", client.post(
            "/api/v1/agreements", json={
                "work_code": "W-002",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": "2027-01-01T00:00:00Z",
                "shares": [
                    {"rightsholder_code": "RH-001", "ratio": "40"},
                    {"rightsholder_code": "RH-002", "ratio": "60"},
                ]}), 201)

        stmt2 = expect_status("adjustment statement", client.post(
            "/api/v1/settlement/statements", json=period), 201)
        check("stmt2 kind adjustment seq1",
              stmt2["kind"] == "adjustment" and stmt2["seq"] == 1, stmt2)
        items2 = {i["rightsholder_code"]: i["amount"] for i in stmt2["items"]}
        # P-0003=250 now payable: RH-001 +100, RH-002 +150; others unchanged.
        check("stmt2 deltas", items2 == {"RH-001": 100, "RH-002": 150}, items2)
        check("stmt2 pending now empty", stmt2["pending"] == [])

        # cumulative totals via prior_total
        comp_after = client.get(
            "/api/v1/settlement/compute"
            "?period_from=2026-01-01T00:00:00Z&period_to=2026-04-01T00:00:00Z"
        ).json()
        prior = {x["rightsholder_code"]: x["amount"] for x in comp_after["prior_total"]}
        cur = {x["rightsholder_code"]: x["amount"] for x in comp_after["payable"]}
        check("prior cumulative = current", prior == cur, (prior, cur))
        check("two prior docs", len(comp_after["prior_documents"]) == 2)

        # another no-change adjustment rejected
        expect_status("second no-change 409", client.post(
            "/api/v1/settlement/statements", json=period), 409)

        # ---- correction that moves play to a different period (negative delta) ----
        # Correct P-JUL (July,100) to a March timestamp -> Q2 loses 100, Q1 gains.
        expect_status("correct P-JUL into Q1", client.post("/api/v1/plays", json={
            "code": "P-JUL-V2", "work_code": "W-001",
            "occurred_at": "2026-03-15T00:00:00Z", "amount": 100,
            "corrects_code": "P-JUL"}), 201)

        # Q1 adjustment: March uses first agreement 50/30/20 -> +50/+30/+20
        stmt3 = expect_status("Q1 adjustment after correction", client.post(
            "/api/v1/settlement/statements", json=period), 201)
        items3 = {i["rightsholder_code"]: i["amount"] for i in stmt3["items"]}
        check("Q1 correction deltas +50/+30/+20",
              items3 == {"RH-001": 50, "RH-002": 30, "RH-003": 20}, items3)

        # Q3 period (July) statement: the tail moved away, so July now has no plays.
        # Issue initial for July BEFORE correction would have been 70/30; now empty ->409
        expect_status("july now empty 409", client.post(
            "/api/v1/settlement/statements", json={
                "period_from": "2026-07-01T00:00:00Z",
                "period_to": "2026-08-01T00:00:00Z"}), 409)

        # ---- statement list / detail ----
        lst = expect_status("statement list", client.get(
            "/api/v1/settlement/statements"), 200)
        check("three statements issued", len(lst) == 3, len(lst))
        check("doc numbers unique", len({s["doc_no"] for s in lst}) == 3)
        detail = expect_status("statement detail", client.get(
            f"/api/v1/settlement/statements/{stmt2['id']}"), 200)
        check("detail matches", detail["doc_no"] == stmt2["doc_no"])
        expect_status("missing statement 404", client.get(
            "/api/v1/settlement/statements/999999"), 404)

        print(f"\n{PASS} passed, {FAIL} failed")
        return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(run())
