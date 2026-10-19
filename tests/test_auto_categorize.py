"""Auto-categorize (slice 17, the user's brief): payees, not transactions, in one batched model call.

Writes payee_categories rows (origin 'llm', one batch_id) for answers at or above
auto_categorize_min_confidence_percent (80). Only payees with no rule and a
recognised Payee are sent; the model picks from the user's own category list or
says none fit. Every answer is remembered, so a second click makes no call.
"""

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from hisaab.config import Settings
from hisaab.db import Database
from hisaab.categories import auto
from hisaab.main import create_app
from hisaab.models import NormalizationPattern, PayeeCategory, PayeeSuggestion
from hisaab.payees import repository as payees
from tests import factories as f
from tests.claude_fake import FakeClaude
from tests.conftest import login, make_user
from tests.pipeline_helpers import BANK_EMAIL, contract, ingest, make_accounts


@pytest.fixture
def claude() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def llm_app(settings: Settings, migrated_engine: Engine, claude: FakeClaude) -> FastAPI:
    app: FastAPI = create_app(settings.model_copy(update={"anthropic_api_key": SecretStr("sk-test")}))
    app.state.llm_transport = claude
    app.state.llm_sleep = lambda seconds: None
    return app


@pytest.fixture
def asha(llm_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(llm_app) as c:
        make_user(llm_app, "asha", "asha password 1", role="admin")
        login(c, "asha", "asha password 1")
        yield c


def db_of(app: FastAPI) -> Database:
    db: Database = app.state.db
    return db


def me(client: TestClient) -> str:
    value: str = client.get("/api/auth/me").json()["id"]
    return value


NAMES: dict[str, str] = {}  # payee id -> name, so a fake reply can answer by the prompt's short references


def payee(app: FastAPI, name: str) -> str:
    with db_of(app).write_session() as s:
        payee_id: str = payees.create_payee(s, name).id
        NAMES[payee_id] = name
        s.add(NormalizationPattern(match_type="contains", pattern=name, payee_id=payee_id, origin="seed"))
        return payee_id


def category(app: FastAPI, user_id: str, name: str) -> str:
    with db_of(app).write_session() as s:
        cat_id: str = f.category(s.connection(), user_id, name=name)["id"]
        return cat_id


def answers(confidences: dict[str, tuple[str | None, Any]]) -> Callable[[str], dict[str, Any]]:
    """A model reply, answering by the prompt's short references: payee id (or "raw:<text>") -> (category, confidence)."""
    def reply(prompt: str) -> dict[str, Any]:
        sent: list[dict[str, Any]] = json.loads(prompt)["payees"]
        ref: dict[str, str] = {p["name"]: p["id"] for p in sent}
        name: Callable[[str], str] = lambda key: key.removeprefix("raw:") if key.startswith("raw:") else NAMES.get(key, key)
        given: list[dict[str, Any]] = [{"payee_id": ref.get(name(key), key), "category_id": c, "confidence": conf}
                                       for key, (c, conf) in confidences.items()]
        answered: set[Any] = {g["payee_id"] for g in given}
        # like the live model: every payee it was sent gets an answer, null where nothing fits
        return {"suggestions": given + [{"payee_id": p["id"], "category_id": None, "confidence": 0.0}
                                        for p in sent if p["id"] not in answered]}
    return reply


def rules(app: FastAPI) -> list[tuple[str, str, str | None, str | None]]:
    with db_of(app).read_session() as s:
        return sorted((r.payee_id, r.category_id, r.origin, r.batch_id) for r in s.scalars(select(PayeeCategory)))


@pytest.fixture
def three(llm_app: FastAPI, asha: TestClient) -> dict[str, str]:
    """Three recognised payees with uncategorized expenses, and two categories."""
    uid: str = me(asha)
    make_accounts(db_of(llm_app), uid)
    ids: dict[str, str] = {name: payee(llm_app, name) for name in ("Swiggy", "Uber", "Mystery Mart")}
    ingest(db_of(llm_app), uid, (contract(payee_raw="SWIGGY ORDER 1", amount=25000), BANK_EMAIL),
           (contract(payee_raw="SWIGGY ORDER 2", amount=45000), BANK_EMAIL),
           (contract(payee_raw="UBER TRIP", amount=31000), BANK_EMAIL),
           (contract(payee_raw="MYSTERY MART 9", amount=9900), BANK_EMAIL))
    ids["food"], ids["travel"] = category(llm_app, uid, "Test Food"), category(llm_app, uid, "Test Travel")
    return ids


def test_one_call_writes_rules_for_confident_payees_only(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                                         three: dict[str, str]) -> None:
    claude.reply = answers({three["Swiggy"]: (three["food"], 0.95), three["Uber"]: (three["travel"], 0.81),
                            three["Mystery Mart"]: (three["food"], 0.5)})
    body: dict[str, Any] = asha.post("/api/review/auto-categorize").json()
    assert claude.calls == 1 and body["called"] is True
    assert (body["asked"], body["transactions"], body["left"]) == (3, 3, 1)
    assert sorted((a["payee_name"], a["confidence_percent"], a["transactions"]) for a in body["applied"]) == \
        [("Swiggy", 95, 2), ("Uber", 81, 1)]
    assert rules(llm_app) == sorted([(three["Swiggy"], three["food"], "llm", body["batch_id"]),
                                     (three["Uber"], three["travel"], "llm", body["batch_id"])])


def test_a_second_click_makes_no_call_and_changes_nothing(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                                          three: dict[str, str]) -> None:
    claude.reply = answers({three["Swiggy"]: (three["food"], 0.95), three["Uber"]: (None, 0.0),
                            three["Mystery Mart"]: (three["food"], 0.5)})
    asha.post("/api/review/auto-categorize")
    before: list[tuple[str, str, str | None, str | None]] = rules(llm_app)
    again: dict[str, Any] = asha.post("/api/review/auto-categorize").json()
    assert claude.calls == 1  # no second request
    assert (again["called"], again["applied"], again["left"]) == (False, [], 2)
    assert rules(llm_app) == before


def test_a_payee_the_user_categorized_is_never_sent_or_changed(llm_app: FastAPI, asha: TestClient,
                                                               claude: FakeClaude, three: dict[str, str]) -> None:
    asha.post("/api/review/payee-category", json={"payee_id": three["Swiggy"], "category_id": three["travel"]})
    claude.reply = answers({three["Swiggy"]: (three["food"], 0.99)})  # even if the model answers it anyway
    asha.post("/api/review/auto-categorize")
    sent: list[str] = [p["name"] for p in json.loads(claude.requests[0]["messages"][0]["content"])["payees"]]
    assert "Swiggy" not in sent and "Uber" in sent
    assert (three["Swiggy"], three["travel"], "user", None) in rules(llm_app)


def test_a_payee_not_yet_named_is_categorized_too_and_named_after_its_text(
    llm_app: FastAPI, asha: TestClient, claude: FakeClaude, three: dict[str, str],
) -> None:
    """User, 2026-10-19: JioHotstar (no Payee, just text) is plainly entertainment; naming is optional."""
    _, [src] = ingest(db_of(llm_app), me(asha), (contract(payee_raw="JioHotstar", amount=15000), BANK_EMAIL))
    claude.reply = answers({"raw:JioHotstar": (three["food"], 0.96)})
    body: dict[str, Any] = asha.post("/api/review/auto-categorize").json()
    sent: dict[str, Any] = json.loads(claude.requests[0]["messages"][0]["content"])
    jio: dict[str, Any] = next(p for p in sent["payees"] if p["name"] == "JioHotstar")
    assert {k: v for k, v in jio.items() if k != "id"} == {
        "name": "JioHotstar", "raw_strings": ["JioHotstar"], "transactions": 1,
        "amount_min_rupees": 150, "amount_max_rupees": 150, "amount_typical_rupees": 150}
    assert [(a["payee_name"], a["transactions"]) for a in body["applied"]] == [("JioHotstar", 1)]
    item: dict[str, Any] = next(i for i in asha.get("/api/review").json()["categorized"] if i["payee_name"] == "JioHotstar")
    assert (item["category_id"], item["origin"]) == (three["food"], "llm")


def test_a_payee_not_yet_named_that_it_couldnt_place_is_not_asked_again(
    llm_app: FastAPI, asha: TestClient, claude: FakeClaude, three: dict[str, str],
) -> None:
    ingest(db_of(llm_app), me(asha), (contract(payee_raw="UPI/DR/1/SOMEONE@okhdfc", amount=700), BANK_EMAIL))
    claude.reply = answers({})
    asha.post("/api/review/auto-categorize")
    assert "SOMEONE" in claude.requests[0]["messages"][0]["content"]
    assert asha.post("/api/review/auto-categorize").json()["called"] is False and claude.calls == 1
    with db_of(llm_app).read_session() as s:  # no name was invented for a payee it couldn't place
        assert s.scalar(select(func.count()).select_from(NormalizationPattern).where(
            NormalizationPattern.origin == "admin")) == 0


def test_the_prompt_carries_the_category_list_raw_strings_count_and_amount_range(
    llm_app: FastAPI, asha: TestClient, claude: FakeClaude, three: dict[str, str],
) -> None:
    claude.reply = answers({})
    asha.post("/api/review/auto-categorize")
    request: dict[str, Any] = claude.requests[0]
    sent: dict[str, Any] = json.loads(request["messages"][0]["content"])
    assert {c["name"] for c in sent["categories"]} >= {"Test Food", "Test Travel"}
    swiggy: dict[str, Any] = next(p for p in sent["payees"] if p["name"] == "Swiggy")
    assert len(swiggy["id"]) <= 4  # a short reference ("p1"), not a long id: every answer line repeats it
    assert (swiggy["name"], sorted(swiggy["raw_strings"]), swiggy["transactions"]) == \
        ("Swiggy", ["SWIGGY ORDER 1", "SWIGGY ORDER 2"], 2)
    assert (swiggy["amount_min_rupees"], swiggy["amount_max_rupees"]) == (250, 450)
    assert request["model"] == "claude-haiku-5-5"
    assert "suggest_categories" in [t["name"] for t in request["tools"]]


@pytest.mark.parametrize("bad", [
    {"category_id": "invented-category", "confidence": 0.99},   # never invent a category
    {"category_id": "FOOD", "confidence": 1.7},                 # confidence outside [0, 1]
    {"category_id": "FOOD", "confidence": True},
    {"category_id": "FOOD", "confidence": "0.9"},
])
def test_an_invented_category_or_odd_confidence_is_treated_as_none_fit(
    llm_app: FastAPI, asha: TestClient, claude: FakeClaude, three: dict[str, str], bad: dict[str, Any],
) -> None:
    reply: dict[str, Any] = {k: (three["food"] if v == "FOOD" else v) for k, v in bad.items()}
    claude.reply = lambda prompt: {"suggestions": [{"payee_id": three["Swiggy"], **reply},
                                                   {"payee_id": "not-asked", "category_id": three["food"],
                                                    "confidence": 0.99}]}
    body: dict[str, Any] = asha.post("/api/review/auto-categorize").json()
    assert body["applied"] == [] and rules(llm_app) == []


def test_over_100_payees_is_two_calls(llm_app: FastAPI, asha: TestClient, claude: FakeClaude) -> None:
    uid: str = me(asha)
    make_accounts(db_of(llm_app), uid)
    names: list[str] = [f"Shop {i:03d}" for i in range(101)]
    for n in names:
        payee(llm_app, n)
    ingest(db_of(llm_app), uid, *[(contract(payee_raw=n, amount=100 + i), BANK_EMAIL) for i, n in enumerate(names)])
    category(llm_app, uid, "Test Food")
    claude.reply = answers({})
    assert asha.post("/api/review/auto-categorize").json()["asked"] == 101
    assert claude.calls == 2


def test_the_model_down_writes_nothing_and_the_next_click_asks_again(
    llm_app: FastAPI, asha: TestClient, claude: FakeClaude, three: dict[str, str],
) -> None:
    claude.fail_next = [529] * 4
    resp: Response = asha.post("/api/review/auto-categorize")
    assert resp.status_code == 503 and "nothing was changed" in resp.json()["detail"]
    assert not {"model", "llm", "confiden"} & set(resp.json()["detail"].lower().split())  # plain words for a person
    assert rules(llm_app) == []
    claude.reply = answers({three["Swiggy"]: (three["food"], 0.9)})
    assert asha.post("/api/review/auto-categorize").json()["called"] is True


def test_undo_removes_only_that_batchs_llm_rows(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                                three: dict[str, str]) -> None:
    claude.reply = answers({three["Swiggy"]: (three["food"], 0.95), three["Uber"]: (three["food"], 0.9)})
    batch: str = asha.post("/api/review/auto-categorize").json()["batch_id"]
    asha.post("/api/review/payee-category", json={"payee_id": three["Uber"], "category_id": three["travel"]})
    assert asha.get("/api/review/auto-categorize/latest").json()["applied"][0]["payee_name"] == "Swiggy"
    assert asha.delete(f"/api/review/auto-categorize/{batch}").json() == {"removed": 1}
    assert rules(llm_app) == [(three["Uber"], three["travel"], "user", None)]  # the one the user took over stays


def test_overrides_still_win_over_an_llm_rule(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                              three: dict[str, str]) -> None:
    item: dict[str, Any] = next(i for i in asha.get("/api/review").json()["items"] if i["payee_id"] == three["Uber"])
    asha.patch(f"/api/transactions/{item['id']}", json={"category_override_id": three["food"]})
    claude.reply = answers({three["Uber"]: (three["travel"], 0.95)})
    asha.post("/api/review/auto-categorize")
    assert asha.get(f"/api/transactions/{item['id']}").json()["category_id"] == three["food"]


def test_adding_a_category_lets_declined_payees_be_asked_again(llm_app: FastAPI, asha: TestClient,
                                                               claude: FakeClaude, three: dict[str, str]) -> None:
    claude.reply = answers({})
    asha.post("/api/review/auto-categorize")
    category(llm_app, me(asha), "Test Shopping")
    asha.post("/api/review/auto-categorize")
    assert claude.calls == 2


def test_a_reply_cut_short_by_the_token_limit_stores_nothing(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                                             three: dict[str, str]) -> None:
    claude.raw_reply = {"type": "message", "stop_reason": "max_tokens", "content": [
        {"type": "tool_use", "name": "suggest_categories", "input": {"suggestions": [
            {"payee_id": three["Swiggy"], "category_id": three["food"], "confidence": 0.99}]}}]}
    assert asha.post("/api/review/auto-categorize").status_code == 503
    assert rules(llm_app) == []
    claude.raw_reply = None
    claude.reply = answers({})
    assert asha.post("/api/review/auto-categorize").json()["asked"] == 3  # nobody was remembered as "none fit"


def test_undo_lets_the_next_click_ask_again(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                            three: dict[str, str]) -> None:
    """User, 2026-10-19: after "Undo auto-categorize", auto-categorize did nothing ever again."""
    claude.reply = answers({three["Swiggy"]: (three["food"], 0.95)})
    batch: str = asha.post("/api/review/auto-categorize").json()["batch_id"]
    asha.delete(f"/api/review/auto-categorize/{batch}")
    again: dict[str, Any] = asha.post("/api/review/auto-categorize").json()
    assert (claude.calls, again["called"], len(again["applied"])) == (2, True, 1)


def test_payees_answered_by_an_older_prompt_are_asked_again(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                                            three: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    claude.reply = answers({})
    asha.post("/api/review/auto-categorize")
    monkeypatch.setattr(auto, "PROMPT_VERSION", auto.PROMPT_VERSION + 1)  # a smarter prompt deserves another look
    assert asha.post("/api/review/auto-categorize").json()["asked"] == 3


def test_it_may_search_the_web_for_payees_it_doesnt_know(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                                         three: dict[str, str]) -> None:
    claude.reply = answers({})
    asha.post("/api/review/auto-categorize")
    request: dict[str, Any] = claude.requests[0]
    assert {"type": "web_search_20250305", "name": "web_search", "max_uses": auto.SEARCHES} in request["tools"]
    assert request["tool_choice"] == {"type": "auto"}  # a forced tool would rule out searching first
    system: str = request["system"]
    assert "Uber" in system and "JioHotstar" in system and "TumbleDry" in system  # common Indian payees, spelled out


def test_a_search_that_pauses_is_resumed_until_it_answers(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                                          three: dict[str, str]) -> None:
    searching: dict[str, Any] = {"type": "message", "stop_reason": "pause_turn", "content": [
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "Mystery Mart India"}}]}
    done: dict[str, Any] = {"type": "message", "stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "suggest_categories", "input": {"suggestions": [
            {"payee_id": three["Mystery Mart"], "category_id": three["food"], "confidence": 0.9}]}}]}
    claude.raw_replies = [searching, done]
    body: dict[str, Any] = asha.post("/api/review/auto-categorize").json()
    assert [a["payee_name"] for a in body["applied"]] == ["Mystery Mart"]
    assert claude.requests[1]["messages"][-1] == {"role": "assistant", "content": searching["content"]}


def test_without_web_search_on_the_account_it_still_categorizes(llm_app: FastAPI, asha: TestClient,
                                                                claude: FakeClaude, three: dict[str, str]) -> None:
    claude.fail_next = [400]  # e.g. web search turned off for the organisation
    claude.reply = answers({three["Uber"]: (three["travel"], 0.97)})
    body: dict[str, Any] = asha.post("/api/review/auto-categorize").json()
    assert [a["payee_name"] for a in body["applied"]] == ["Uber"]
    assert "web_search" not in json.dumps(claude.requests[1]["tools"])


def test_answers_sent_back_as_json_text_are_still_read(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                                       three: dict[str, str]) -> None:
    """User, 2026-10-19: the live model returned the list as a JSON string; all 190 answers were dropped."""
    as_list: Callable[[str], dict[str, Any]] = answers({three["Uber"]: (three["travel"], 0.95)})
    claude.reply = lambda prompt: {"suggestions": json.dumps(as_list(prompt)["suggestions"])}
    body: dict[str, Any] = asha.post("/api/review/auto-categorize").json()
    assert [a["payee_name"] for a in body["applied"]] == ["Uber"]


def test_a_reply_with_no_usable_answer_stores_nothing_and_says_so(llm_app: FastAPI, asha: TestClient,
                                                                  claude: FakeClaude, three: dict[str, str]) -> None:
    claude.reply = lambda prompt: {"suggestions": "I could not do this"}
    assert asha.post("/api/review/auto-categorize").status_code == 503
    with db_of(llm_app).read_session() as s:  # no memory written: the next click asks again
        assert s.scalar(select(func.count()).select_from(PayeeSuggestion)) == 0
    claude.reply = answers({three["Uber"]: (three["travel"], 0.95)})
    assert asha.post("/api/review/auto-categorize").json()["called"] is True


def test_the_answer_tool_is_strict(llm_app: FastAPI, asha: TestClient, claude: FakeClaude,
                                   three: dict[str, str]) -> None:
    claude.reply = answers({})
    asha.post("/api/review/auto-categorize")
    tool: dict[str, Any] = next(t for t in claude.requests[0]["tools"] if t["name"] == "suggest_categories")
    assert tool["strict"] is True and tool["input_schema"]["additionalProperties"] is False
    assert "minimum" not in json.dumps(tool["input_schema"])  # strict schemas take no numeric bounds
