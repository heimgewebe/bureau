from pathlib import Path

SERVICE = Path(__file__).parents[1] / "ops/systemd/bureau-agent-frontier.service"
TIMER = Path(__file__).parents[1] / "ops/systemd/bureau-agent-frontier.timer"


def test_agent_frontier_service_is_read_only_except_frontier_state():
    text = SERVICE.read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in text
    assert "ProtectHome=read-only" in text
    assert "ReadWritePaths=%h/.local/state/bureau-agent-frontier" in text
    assert "NoNewPrivileges=true" in text
    assert "bureau-agent-frontier" in text


def test_agent_frontier_timer_runs_after_closure_before_operator():
    text = TIMER.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* *:55:00" in text
    assert "Unit=bureau-agent-frontier.service" in text
