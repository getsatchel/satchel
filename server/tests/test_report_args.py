"""report_answer accepts the argument names agents commonly guess."""
import pytest
from pydantic import ValidationError

from satchel.tools.definitions import ReportArgs


def test_answer_and_summary_stand_in_for_answer_summary():
    for name in ("answer", "summary"):
        a = ReportArgs.model_validate(
            {"session_id": "s", name: "40 customers", "confidence": "high",
             "caveats": "Data ends Oct 2024."}
        )
        assert a.answer_summary == "40 customers"
        assert a.caveats == ["Data ends Oct 2024."]


def test_answer_summary_wins_over_a_guess():
    a = ReportArgs.model_validate(
        {"session_id": "s", "answer_summary": "real", "answer": "other", "confidence": "low"}
    )
    assert a.answer_summary == "real"


def test_confidence_is_still_required():
    with pytest.raises(ValidationError, match="confidence"):
        ReportArgs.model_validate({"session_id": "s", "answer": "x"})
