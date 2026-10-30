import pytest
from pydantic_ai import ModelRetry

from agent_lab.agents.demo import calculate, current_time, lookup_note, safe_eval


@pytest.mark.parametrize("expr,expected", [("1+2*3", 7), ("(1+2)*3", 9), ("-4/2", -2), ("2.5*2", 5)])
def test_safe_eval(expr, expected):
    assert safe_eval(expr) == expected


@pytest.mark.parametrize("expr", ["__import__('os')", "2**100000", "a+1", "[1]", "1 if 1 else 2", "x" * 300])
def test_safe_eval_rejects_unsafe(expr):
    with pytest.raises((ValueError, SyntaxError)):
        safe_eval(expr)


def test_calculate_asks_model_to_retry_on_bad_input():
    with pytest.raises(ModelRetry):
        calculate("1/0")


def test_current_time_and_bad_timezone():
    assert current_time("UTC").endswith("+00:00")
    with pytest.raises(ModelRetry):
        current_time("Mars/Olympus")


def test_lookup_note():
    assert "container" in lookup_note("Docker")
    with pytest.raises(ModelRetry):
        lookup_note("unknown")
