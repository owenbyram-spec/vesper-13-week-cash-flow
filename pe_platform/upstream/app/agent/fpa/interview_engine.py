"""Typed human decisions for the workbench. Display labels never execute arithmetic."""
from __future__ import annotations

from copy import deepcopy
import math

from ...fpa.uaf.schema import UniversalTrialBalance
from .anomaly_detector import detect_tb_anomalies
from .interview_state import InterviewQuestion, InterviewSession, QuestionCategory, SessionStatus
from .model_inputs import BUCKETS, ContractError, WorkbenchModelInputs
from .modes import ForecastMode


def _option(id, label, value):
    return {"option_id": id, "label": label, "value": value}


def generate_interview_questions(tb, anomalies, mode=ForecastMode.OPERATING):
    questions = []
    seen = set()

    def add(key, category, title, options, account_id=None):
        if key in seen:
            return
        seen.add(key)
        account = tb.accounts.get(account_id)
        bal = tb.balances.get(account_id)
        questions.append(InterviewQuestion(
            question_id=key, decision_key=key, category=category, title=title,
            prompt="Select an explicit modeling assumption. Suggestions are not verified financial facts.",
            options=options, recommended_option=options[0]["option_id"], confidence=0.0,
            impact_rationale="Accepted inputs affect the deterministic forecast; review their provenance.",
            account_id=account_id, account_name=account.name if account else None,
            dollar_magnitude=abs(bal.net_balance) if bal else 0,
            target={"kind": category.value, "account_id": account_id},
        ))

    for anomaly in anomalies:
        aid = anomaly.account_id
        if aid not in tb.accounts:
            continue
        if anomaly.anomaly_type in ("ambiguous_classification", "unknown_classification"):
            add("account_classification:" + aid, QuestionCategory.COA_CLASSIFICATION,
                "Classification: " + tb.accounts[aid].name,
                [_option(b, b.replace("_", " "), b) for b in ["cogs", "opex", "contra_revenue"] +
                 [b for b in BUCKETS if b not in ("cogs", "opex", "contra_revenue")]], aid)
        if anomaly.anomaly_type == "candidate_addback":
            add("ebitda_addback:" + aid, QuestionCategory.EBITDA_ADDBACK,
                "EBITDA adjustment: " + tb.accounts[aid].name, [
                    _option("none", "No adjustment", {"pct": 0.0, "treatment": "valuation_only"}),
                    *[_option(f"{pct}_{t}", f"{pct}% — {t.replace('_', ' ')}",
                              {"pct": pct / 100, "treatment": t})
                      for pct in (100, 50) for t in ("valuation_only", "remove_recurring_cost")],
                ], aid)
        if anomaly.anomaly_type == "debt_tranche":
            treatments = (["payoff_at_close", "retain", "equity_conversion"]
                          if mode == ForecastMode.TRANSACTION_LBO
                          else ["contractual", "interest_only", "accelerated"])
            add("debt_treatment:" + aid, QuestionCategory.DEBT_STRUCTURE,
                "Debt treatment: " + tb.accounts[aid].name,
                [_option(t, t.replace("_", " "), t) for t in treatments], aid)
    for key, title, values in [
        ("revenue_growth_cagr", "Annual revenue growth", [("growth_8", "8%", .08), ("growth_5", "5%", .05), ("growth_12", "12%", .12)]),
        ("capex_pct", "Annual capital expenditure / revenue", [("capex_3_5", "3.5%", .035), ("capex_2", "2%", .02), ("capex_5", "5%", .05)]),
        ("min_cash_floor", "Minimum cash balance", [("cash_1000000", "$1,000,000", 1_000_000.), ("cash_500000", "$500,000", 500_000.), ("cash_1500000", "$1,500,000", 1_500_000.)]),
    ]:
        add(key, QuestionCategory.OPERATING_BUDGET if key == "min_cash_floor" else QuestionCategory.DRIVER_ASSUMPTION,
            title, [_option(*v) for v in values] + [_option("custom", "Explicit numeric value", None)])
    return questions


def start_interview_session(tb: UniversalTrialBalance, entity_id=None, mode=ForecastMode.OPERATING):
    mode = ForecastMode(mode)
    # Own an immutable ingestion snapshot, never mutate the caller's UAF object.
    original = deepcopy(tb)
    anomalies = detect_tb_anomalies(original)
    session = InterviewSession(entity_id=entity_id or tb.entity_id, mode=mode, trial_balance=original,
                               anomalies=anomalies, questions=generate_interview_questions(original, anomalies, mode))
    session.log_event("session_started", {"entity_id": session.entity_id, "mode": mode.value,
                                        "computation_contract_version": 2})
    return session


def assert_mutable(session):
    if session.schema_version != 2:
        raise ContractError("interview_schema_upgrade_required", [], "Restart this legacy interview and accept typed inputs.")
    if session.status == SessionStatus.LOCKED:
        raise ContractError("interview_locked", [], "Locked interviews are immutable.")


def _rebuild(session):
    session.resolved_classifications = {}
    session.ebitda_addbacks = []
    session.debt_tranche_definitions = []
    session.driver_overrides = {}
    for q in session.questions:
        decision = session.decisions.get(q.decision_key)
        if not decision:
            continue
        value = decision["value"]
        if q.category == QuestionCategory.COA_CLASSIFICATION:
            session.resolved_classifications[q.account_id] = value
        elif q.category == QuestionCategory.EBITDA_ADDBACK and value["pct"]:
            session.ebitda_addbacks.append({"account_id": q.account_id, "account_name": q.account_name,
                "amount": round(q.dollar_magnitude * value["pct"], 2), **value, "provenance": decision["provenance"]})
        elif q.category == QuestionCategory.DEBT_STRUCTURE:
            session.debt_tranche_definitions.append({"account_id": q.account_id, "treatment": value})
        elif q.decision_key in ("revenue_growth_cagr", "capex_pct", "min_cash_floor"):
            session.driver_overrides[q.decision_key] = value
    session.status = SessionStatus.RESOLVED if session.pending_questions_count() == 0 else SessionStatus.IN_PROGRESS


def answer_interview_question(session, question_id, option_id, user_notes=None, *, value=None,
                              actor_id=None, revision=None):
    assert_mutable(session)
    q = session.get_question(question_id)
    if not q or not q.decision_key or q.question_id != q.decision_key:
        raise ContractError("invalid_interview_answer", ["question_id"], "Unknown question ID.")
    if q.account_id and q.account_id not in session.trial_balance.accounts:
        raise ContractError("invalid_interview_answer", ["account_id"], "Unknown account target.")
    option = next((o for o in q.options if o["option_id"] == option_id), None)
    if option is None:
        raise ContractError("invalid_interview_answer", ["option_id"], "Unknown typed option ID.")
    if option_id == "custom":
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ContractError("invalid_interview_answer", ["value"], "A finite numeric value is required.")
        lower, upper = {"revenue_growth_cagr": (-1, 10), "capex_pct": (0, 1), "min_cash_floor": (0, 1e15)}[q.decision_key]
        if not lower <= value <= upper:
            raise ContractError("invalid_interview_answer", ["value"], "Numeric value is outside the supported range.")
        accepted = float(value)
    else:
        if value is not None:
            raise ContractError("invalid_interview_answer", ["value"], "A named option owns its typed value; do not supply another value.")
        accepted = deepcopy(option["value"])
    # All checks precede mutation. Targets and option values originate on the server.
    provenance = {"kind": "user_assumption", "actor_id": actor_id, "revision": revision,
                  "decision_id": q.decision_key, "source_refs": [q.account_id] if q.account_id else [],
                  "period": session.trial_balance.as_of_date.isoformat()}
    session.decisions[q.decision_key] = {"option_id": option_id, "value": accepted, "provenance": provenance}
    q.selected_answer, q.selected_value, q.user_notes, q.is_answered = option_id, accepted, user_notes, True
    _rebuild(session)
    session.log_event("question_answered", {"question_id": question_id, "option_id": option_id, "value": accepted,
                                            "provenance": provenance})
    return q


def auto_resolve_high_confidence(session, accepted_recommendations=None, *, actor_id=None, revision=None, **old):
    """Explicit acceptance only. Confidence thresholds are never human authority."""
    assert_mutable(session)
    if old or not isinstance(accepted_recommendations, dict) or not accepted_recommendations:
        raise ContractError("explicit_acceptance_required", ["accepted_recommendations"],
                            "Submit question IDs and their recommended option IDs at the current revision.")
    candidate = deepcopy(session)
    for qid, oid in accepted_recommendations.items():
        q = candidate.get_question(qid)
        if q is None or oid != q.recommended_option:
            raise ContractError("invalid_interview_answer", [qid], "Only the named recommendation can be accepted in this batch.")
        answer_interview_question(candidate, qid, oid, actor_id=actor_id, revision=revision)
    session.__dict__.update(candidate.__dict__)
    return len(accepted_recommendations)


def accept_model_inputs(session, inputs: WorkbenchModelInputs, *, actor_id=None, revision=None):
    assert_mutable(session)
    values = {k: v for k, v in inputs.model_dump().items() if v is not None}
    accounts = set(session.trial_balance.accounts)
    refs = set((inputs.account_mappings or {}).keys()) | {d.account_id for d in inputs.debt_facilities or [] if d.account_id}
    conflicts = [aid for aid, bucket in (inputs.account_mappings or {}).items()
                 if aid in session.resolved_classifications and session.resolved_classifications[aid] != bucket]
    if conflicts:
        raise ContractError("invalid_model_inputs", ["account_mappings." + aid for aid in conflicts],
                            "Mapping conflicts with an accepted classification answer; reanswer that decision first.")
    if not refs <= accounts:
        raise ContractError("invalid_model_inputs", sorted(refs - accounts), "Unknown account references.")
    if inputs.sweep_pct_by_year is not None and any(not 0 <= v <= 1 for v in inputs.sweep_pct_by_year):
        raise ContractError("invalid_model_inputs", ["sweep_pct_by_year"], "Sweep percentages must be within 0 and 1.")
    # Whole replacement at a revision, preventing stale fragments from surviving a policy change.
    session.model_inputs = values
    session.input_provenance = {"kind": "user_assumption", "actor_id": actor_id, "revision": revision,
        "decision_id": "model_inputs", "source_refs": sorted(refs), "period": session.trial_balance.as_of_date.isoformat(),
        "accepted_fields": sorted(values), "values": deepcopy(values)}
    session.log_event("model_inputs_accepted", session.input_provenance)
