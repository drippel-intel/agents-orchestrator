from __future__ import annotations

import pytest

from agents_orchestrator.agents.qa import QAReportError, parse_qa_report


def test_parse_qa_report_accepts_fenced_json() -> None:
    report = parse_qa_report(
        """
        ```json
        {
          "passed": false,
          "summary": "DAX lint failed.",
          "failures": ["Measure has invalid syntax"],
          "recommendations": ["Fix the measure expression"]
        }
        ```
        """
    )

    assert report["passed"] is False
    assert report["failures"] == ["Measure has invalid syntax"]


def test_parse_qa_report_requires_passed() -> None:
    with pytest.raises(QAReportError):
        parse_qa_report('{"summary": "missing status"}')
