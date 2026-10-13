"""Week 12: the triage desk's MCP tools, called the way another squad's client would call them -- over the wire."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.limits import LimitsTable
from app.mcp_access import AccessPolicy, Principal
from app.mcp_client import McpToolClient
from app.mcp_server import TRIAGE_TOOLS, DeskServices, build_server_for_service
from app.mcp_vetting import VetPolicy, vet_tools
from app.observability import JsonlSink, Tracer
from app.policy_registry import PolicyRegistry
from app.rag import RagService
from app.referrals import ReferralQueue
from app.registry import DocumentRegistry
from scripts.make_sample_pdf import build_form_library_pdf
from tests.test_mcp import serve_http

POLICY = "GML-2024-88213"
WINDOW_RE = re.compile(r"within (\d+) consecutive days")


@pytest.fixture
def service(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path) -> RagService:
    cfg = settings.model_copy(update={"chroma_dir": tmp_path / "chroma"})
    svc = RagService(cfg, registry=DocumentRegistry(cfg.registry_path))
    svc.ingest(sample_pdf_bytes, "sample_endorsement_pack.pdf")
    library = tmp_path / "archive.pdf"
    build_form_library_pdf(library)
    svc.ingest(library.read_bytes(), "form_ho0304_archive.pdf")
    return svc


@pytest.fixture
def desk(tmp_path: Path) -> DeskServices:
    return DeskServices(PolicyRegistry.from_file(), LimitsTable.from_file(), ReferralQueue(tmp_path / "referrals.jsonl"))


@pytest.fixture
def client(service: RagService, desk: DeskServices) -> Iterator[McpToolClient]:
    connected = McpToolClient(build_server_for_service(service, desk=desk, enabled=TRIAGE_TOOLS)).connect()
    yield connected
    connected.close()


def call(client: McpToolClient, tool: str, **arguments: Any):
    return client.call_tool(tool, arguments)


# --------------------------------------------------------------------------- #
# Discovery and vetting
# --------------------------------------------------------------------------- #
def test_the_five_triage_tools_are_discoverable_and_only_one_writes(client: McpToolClient):
    tools = {t.name: t for t in client.list_tools()}
    assert set(tools) == set(TRIAGE_TOOLS)
    assert [name for name, t in tools.items() if t.read_only is not True] == ["refer_to_adjuster"]
    assert tools["refer_to_adjuster"].destructive is False


def test_a_write_tool_is_vetted_in_only_when_the_operator_names_it(client: McpToolClient):
    tools = client.list_tools()
    strict, verdicts = vet_tools(tools, VetPolicy())
    assert "refer_to_adjuster" not in {t.name for t in strict}
    assert next(v for v in verdicts if v.tool == "refer_to_adjuster").reasons == ["does not declare itself read-only"]
    permitted, _ = vet_tools(tools, VetPolicy(write_allowed=frozenset({"refer_to_adjuster"})))
    assert {t.name for t in permitted} == set(TRIAGE_TOOLS)


def test_a_triage_tool_cannot_be_enabled_without_the_desk(service: RagService):
    with pytest.raises(ValueError, match="need a DeskServices"):
        build_server_for_service(service, enabled=["lookup_policy"])


# --------------------------------------------------------------------------- #
# lookup_policy / coverage_in_force
# --------------------------------------------------------------------------- #
def test_lookup_by_claim_number_returns_the_policy_and_whether_its_wording_is_on_file(client: McpToolClient):
    found = call(client, "lookup_policy", reference="CLM-2024-00417").structured
    assert (found["found"], found["policy_number"], found["claim_number"]) == (True, POLICY, "CLM-2024-00417")
    assert found["wording_on_file"] is True and found["period_start"] == "2024-04-01"
    no_wording = call(client, "lookup_policy", reference="GML-2023-61450").structured
    assert no_wording["found"] and no_wording["wording_on_file"] is False


def test_an_unknown_reference_is_found_false_and_a_malformed_one_never_reaches_the_handler(client: McpToolClient):
    assert call(client, "lookup_policy", reference="CLM-2099-99999").structured["found"] is False
    assert call(client, "lookup_policy", reference="Priya Rao, 14 Station Road").is_error  # not an identifier


@pytest.mark.parametrize("day, expected, reason", [("2025-03-31", True, "in_force"), ("2025-04-01", False, "after_expiry"), ("2024-03-31", False, "before_inception")])
def test_coverage_in_force_over_the_wire(client: McpToolClient, day: str, expected: bool, reason: str):
    result = call(client, "coverage_in_force", policy_number=POLICY, loss_date=day).structured
    assert (result["in_force"], result["reason_code"]) == (expected, reason)


def test_a_date_that_is_not_a_date_is_a_tool_error_not_a_crash(client: McpToolClient):
    assert call(client, "coverage_in_force", policy_number=POLICY, loss_date="2024-02-30").is_error
    assert call(client, "coverage_in_force", policy_number=POLICY, loss_date="11 August 2024").is_error


# --------------------------------------------------------------------------- #
# search_wording
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("day, window, edition", [("2023-12-15", "45", "09-23"), ("2024-08-11", "55", "01-24"), ("2025-01-31", "55", "01-24"), ("2025-02-01", "60", "03-24")])
def test_search_wording_returns_only_the_edition_in_force_and_names_it(client: McpToolClient, day: str, window: str, edition: str):
    # 2023-12-15 predates the policy's inception, but search_wording answers "what wording applied that day" -- in-force is a separate tool
    result = call(client, "search_wording", query="E-17 second flood within consecutive days", policy_number=POLICY, loss_date=day, top_k=4).structured
    text = " ".join(p["text"] for p in result["passages"])
    assert set(WINDOW_RE.findall(" ".join(text.split()))) == {window}
    assert result["editions_in_force"] == [edition] and result["loss_date"] == day
    assert all(p["versions"] for p in result["passages"] if "E-17" in p["text"])


def test_search_wording_cannot_be_called_without_a_policy_and_a_loss_date(client: McpToolClient):
    assert call(client, "search_wording", query="E-17").is_error
    assert call(client, "search_wording", query="E-17", policy_number=POLICY).is_error
    assert call(client, "search_wording", query="E-17", loss_date="2024-08-11").is_error


def test_search_wording_searches_nothing_for_a_policy_with_no_wording_on_file(client: McpToolClient):
    result = call(client, "search_wording", query="flood", policy_number="GML-2023-61450", loss_date="2023-11-02").structured
    assert result["passages"] == [] and "no wording is on file" in result["note"]
    unknown = call(client, "search_wording", query="flood", policy_number="GML-2099-00001", loss_date="2024-08-11").structured
    assert unknown["passages"] == [] and "no such policy" in unknown["note"]


def test_search_wording_does_not_echo_the_query_back(client: McpToolClient):
    query = "Priya Rao of 14 Station Road, SW1A 1AA, broke her wrist; flood E-17"
    raw = json.dumps(call(client, "search_wording", query=query, policy_number=POLICY, loss_date="2024-08-11").structured)
    assert "Priya" not in raw and "Station Road" not in raw and "wrist" not in raw


# --------------------------------------------------------------------------- #
# get_limits
# --------------------------------------------------------------------------- #
def test_get_limits_returns_three_separate_numbers_with_their_bases(client: McpToolClient):
    limits = call(client, "get_limits", policy_number=POLICY, loss_date="2024-08-11", peril="flood", in_flood_zone=True).structured
    assert (limits["deductible"]["amount"], limits["sub_limit"]["amount"], limits["policy_limit"]["amount"]) == (250_000, 5_000_000, 40_000_000)
    assert limits["sub_limit"]["basis"] == "annual aggregate" and limits["deductible"]["basis"] == "each and every loss"


def test_get_limits_refuses_to_pick_a_deductible_when_the_flood_zone_is_unknown(client: McpToolClient):
    limits = call(client, "get_limits", policy_number=POLICY, loss_date="2024-08-11", peril="flood").structured
    assert limits["deductible"]["amount"] is None and limits["unresolved"] == ["in_flood_zone"]
    assert sorted(c["amount"] for c in limits["deductible"]["candidates"]) == [50_000, 250_000]


def test_get_limits_follows_the_loss_date(client: McpToolClient):
    before = call(client, "get_limits", policy_number=POLICY, loss_date="2024-05-31", peril="flood", in_flood_zone=False).structured
    after = call(client, "get_limits", policy_number=POLICY, loss_date="2024-06-01", peril="flood", in_flood_zone=False).structured
    assert (before["sub_limit"]["amount"], after["sub_limit"]["amount"]) == (2_500_000, 5_000_000)


# --------------------------------------------------------------------------- #
# refer_to_adjuster
# --------------------------------------------------------------------------- #
REFERRAL = dict(claim_number="CLM-2024-00417", policy_number=POLICY, loss_date="2024-08-11", reason_codes=["exclusion_arguable"], facts="Second flood 40 days after the first; E-17 window 55 days (edition 01-24).")


def test_referral_is_idempotent_one_row_however_many_calls(client: McpToolClient, desk: DeskServices):
    first = call(client, "refer_to_adjuster", **REFERRAL).structured
    again = call(client, "refer_to_adjuster", **REFERRAL).structured
    assert first["referral_id"] == again["referral_id"] and (first["duplicate"], again["duplicate"]) == (False, True)
    assert len(desk.referrals.records()) == 1


def test_a_referral_that_states_a_decision_is_refused_and_nothing_is_queued(client: McpToolClient, desk: DeskServices):
    for notes in ("This flood claim is covered under Endorsement 1.", "Claim will be paid in full.", "The E-17 exclusion applies, so the claim is declined."):
        outcome = call(client, "refer_to_adjuster", **{**REFERRAL, "facts": notes})
        assert outcome.is_error and "may not state a coverage decision" in outcome.text
    assert desk.referrals.records() == []


def test_personal_details_are_redacted_before_the_queue_is_written(client: McpToolClient, desk: DeskServices, tmp_path: Path):
    facts = "Claimant Priya Rao of 14 Station Road, SW1A 1AA reports the flood. She broke her wrist. Second flood 40 days after the first."
    call(client, "refer_to_adjuster", **{**REFERRAL, "facts": facts})
    on_disk = (tmp_path / "referrals.jsonl").read_text(encoding="utf-8")
    assert "Priya" not in on_disk and "Station Road" not in on_disk and "SW1A" not in on_disk and "wrist" not in on_disk
    assert "Second flood 40 days after the first" in on_disk  # the facts the adjuster needs survive


def test_an_unknown_reason_code_is_rejected_by_the_schema(client: McpToolClient):
    assert call(client, "refer_to_adjuster", **{**REFERRAL, "reason_codes": ["looks_fine"]}).is_error
    assert call(client, "refer_to_adjuster", **{**REFERRAL, "reason_codes": []}).is_error


def test_the_referral_carries_the_trace_id_of_the_request_that_made_it(client: McpToolClient, desk: DeskServices, tmp_path: Path):
    tracer = Tracer(JsonlSink(tmp_path / "t.jsonl"))
    with tracer.trace("triage") as active:
        call(client, "refer_to_adjuster", **REFERRAL)
    assert desk.referrals.records()[0]["trace_id"] == active.trace_id


# --------------------------------------------------------------------------- #
# Scope: who may call what
# --------------------------------------------------------------------------- #
def test_a_partner_squad_with_read_scope_cannot_see_or_call_the_write_tool(service: RagService, desk: DeskServices):
    triage_token, partner_token = "t" * 32, "p" * 32
    policy = AccessPolicy.from_tokens(
        {
            triage_token: Principal("triage-desk"),
            partner_token: Principal("partner-squad", frozenset({"search_wording", "coverage_in_force"})),
        }
    )
    server = build_server_for_service(service, desk=desk, enabled=TRIAGE_TOOLS, access=policy, transport="http")
    with serve_http(server, policy) as url:
        with McpToolClient(url, headers={"Authorization": f"Bearer {partner_token}"}) as partner:
            assert {t.name for t in partner.list_tools()} == {"search_wording", "coverage_in_force"}
            denied = partner.call_tool("refer_to_adjuster", REFERRAL)
            ghost = partner.call_tool("no_such_tool", REFERRAL)
            assert denied.is_error and denied.text.replace("refer_to_adjuster", "X") == ghost.text.replace("no_such_tool", "X")
            assert not partner.call_tool("coverage_in_force", {"policy_number": POLICY, "loss_date": "2024-08-11"}).is_error
        with McpToolClient(url, headers={"Authorization": f"Bearer {triage_token}"}) as triage:
            assert not triage.call_tool("refer_to_adjuster", REFERRAL).is_error
    assert [r["referred_by"] for r in desk.referrals.records()] == ["triage-desk"]  # the principal is recorded, from the token


def test_get_limits_gives_nothing_to_a_policy_issued_on_wording_that_does_not_state_the_figures(client: McpToolClient):
    limits = call(client, "get_limits", policy_number="GML-2023-55120", loss_date="2023-12-15", peril="flood", in_flood_zone=True).structured
    assert (limits["deductible"]["amount"], limits["sub_limit"]["amount"], limits["policy_limit"]["amount"]) == (None, None, None)
