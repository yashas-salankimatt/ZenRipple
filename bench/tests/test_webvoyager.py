"""Tests for WebVoyager loader and judge integration."""

import json
import tempfile
from pathlib import Path

import pytest

from bench.loaders.webvoyager import (
    WebVoyagerTask,
    _normalize_task,
    load_tasks,
    tasks_to_scenarios,
)
from bench.scenario import ScenarioCategory


# --- _normalize_task ---


class TestNormalizeTask:
    def test_format_a_original(self):
        raw = {
            "task_id": "allrecipes_0",
            "web_name": "allrecipes",
            "task": "Find a cookie recipe",
            "url": "https://www.allrecipes.com/",
        }
        t = _normalize_task(raw, 0)
        assert t.task_id == "allrecipes_0"
        assert t.website == "allrecipes"
        assert t.intent == "Find a cookie recipe"
        assert t.url == "https://www.allrecipes.com/"

    def test_format_b_variant(self):
        raw = {
            "id": "flights-3",
            "website": "Google Flights",
            "intent": "Book a flight to LA",
            "start_url": "https://www.google.com/flights",
        }
        t = _normalize_task(raw, 0)
        assert t.task_id == "flights-3"
        assert t.website == "Google Flights"
        assert t.intent == "Book a flight to LA"
        assert t.url == "https://www.google.com/flights"

    def test_format_c_ques_style(self):
        raw = {
            "id": 7,
            "web": "GitHub",
            "ques": "Star the repo",
            "url": "https://github.com/",
        }
        t = _normalize_task(raw, 0)
        assert t.task_id == "7"
        assert t.website == "GitHub"
        assert t.intent == "Star the repo"

    def test_fallback_id(self):
        raw = {"task": "Do something", "url": "https://example.com"}
        t = _normalize_task(raw, 42)
        assert t.task_id == "wv-42"
        assert t.website == "unknown"

    def test_missing_intent_raises(self):
        raw = {"url": "https://example.com"}
        with pytest.raises(ValueError, match="no intent"):
            _normalize_task(raw, 0)

    def test_missing_url_raises(self):
        raw = {"task": "Do something"}
        with pytest.raises(ValueError, match="no url"):
            _normalize_task(raw, 0)


# --- load_tasks ---


class TestLoadTasks:
    def test_load_valid_file(self, tmp_path: Path):
        data = [
            {
                "task_id": "t1",
                "web_name": "site",
                "task": "Do X",
                "url": "https://example.com",
            },
            {
                "task_id": "t2",
                "web_name": "site",
                "task": "Do Y",
                "url": "https://example.com/y",
            },
        ]
        f = tmp_path / "tasks.json"
        f.write_text(json.dumps(data))

        tasks = load_tasks(f)
        assert len(tasks) == 2
        assert tasks[0].task_id == "t1"
        assert tasks[1].intent == "Do Y"

    def test_load_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_tasks("/nonexistent/tasks.json")

    def test_load_non_array(self, tmp_path: Path):
        f = tmp_path / "bad.json"
        f.write_text('{"not": "an array"}')
        with pytest.raises(ValueError, match="JSON array"):
            load_tasks(f)

    def test_load_empty_array(self, tmp_path: Path):
        f = tmp_path / "empty.json"
        f.write_text("[]")
        tasks = load_tasks(f)
        assert tasks == []


# --- tasks_to_scenarios ---


class TestTasksToScenarios:
    def _sample_tasks(self) -> list[WebVoyagerTask]:
        return [
            WebVoyagerTask(
                task_id="allrecipes_0",
                website="Allrecipes",
                url="https://www.allrecipes.com/",
                intent="Find a recipe for chocolate chip cookies",
            ),
            WebVoyagerTask(
                task_id="github_1",
                website="GitHub",
                url="https://github.com/",
                intent="Find the most starred Python repo",
            ),
        ]

    def test_correct_count(self):
        scenarios = tasks_to_scenarios(self._sample_tasks())
        assert len(scenarios) == 2

    def test_scenario_ids(self):
        scenarios = tasks_to_scenarios(self._sample_tasks())
        assert scenarios[0].id == "wv-allrecipes_0"
        assert scenarios[1].id == "wv-github_1"

    def test_scenario_tags(self):
        scenarios = tasks_to_scenarios(self._sample_tasks())
        assert "webvoyager" in scenarios[0].tags
        assert "allrecipes" in scenarios[0].tags
        assert "github" in scenarios[1].tags

    def test_scenario_category(self):
        scenarios = tasks_to_scenarios(self._sample_tasks())
        for s in scenarios:
            assert s.category == ScenarioCategory.MULTI_STEP

    def test_scenario_prompt_contains_intent(self):
        scenarios = tasks_to_scenarios(self._sample_tasks())
        assert "chocolate chip cookies" in scenarios[0].prompt

    def test_scenario_prompt_contains_url(self):
        scenarios = tasks_to_scenarios(self._sample_tasks())
        assert "https://www.allrecipes.com/" in scenarios[0].prompt

    def test_scenario_has_judge_verification(self):
        scenarios = tasks_to_scenarios(self._sample_tasks())
        for s in scenarios:
            assert len(s.verifications) == 1
            assert s.verifications[0].description == "WebVoyager LLM judge"

    def test_custom_limits(self):
        scenarios = tasks_to_scenarios(
            self._sample_tasks(),
            max_turns=50,
            max_budget_usd=2.00,
            timeout_seconds=600,
        )
        for s in scenarios:
            assert s.max_turns == 50
            assert s.max_budget_usd == 2.00
            assert s.timeout_seconds == 600

    def test_default_limits(self):
        scenarios = tasks_to_scenarios(self._sample_tasks())
        for s in scenarios:
            assert s.max_turns == 30
            assert s.max_budget_usd == 1.00
            assert s.timeout_seconds == 300
            assert s.difficulty == "hard"
