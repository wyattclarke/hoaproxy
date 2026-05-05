import json

from hoaware import model_usage
from hoaware import doc_classifier


class _Usage:
    prompt_tokens = 12
    completion_tokens = 4
    total_tokens = 16


class _Choice:
    finish_reason = "stop"


class _Response:
    id = "gen-test"
    usage = _Usage()
    choices = [_Choice()]


def test_log_llm_call_writes_metadata_only(tmp_path, monkeypatch):
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("HOA_OPENROUTER_GENERATION_LOOKUP", "0")

    model_usage.log_llm_call(
        operation="test.operation",
        model="deepseek/deepseek-v4-flash",
        api_base_url="https://openrouter.ai/api/v1",
        response=_Response(),
        elapsed_ms=123,
        metadata={"source_url": "https://example.test/doc.pdf", "filename": "doc.pdf"},
        log_path=log_path,
    )

    row = json.loads(log_path.read_text())
    assert row["operation"] == "test.operation"
    assert row["model"] == "deepseek/deepseek-v4-flash"
    assert row["generation_id"] == "gen-test"
    assert row["usage"]["prompt_tokens"] == 12
    assert row["usage"]["completion_tokens"] == 4
    assert row["metadata"]["filename"] == "doc.pdf"
    assert "prompt" not in row
    assert "completion" not in row


def test_classifier_defaults_to_deepseek(monkeypatch):
    monkeypatch.delenv("HOA_CLASSIFIER_MODEL", raising=False)
    monkeypatch.delenv("HOA_CLASSIFIER_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("HOA_ALLOW_BLOCKLISTED_CLASSIFIER_MODELS", raising=False)

    assert doc_classifier._classifier_models() == ["deepseek/deepseek-v4-flash"]


def test_classifier_blocklists_qwen_by_default(monkeypatch):
    monkeypatch.setenv("HOA_CLASSIFIER_MODEL", "qwen/qwen3.5-flash-02-23")
    monkeypatch.setenv("HOA_CLASSIFIER_FALLBACK_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.delenv("HOA_ALLOW_BLOCKLISTED_CLASSIFIER_MODELS", raising=False)

    assert doc_classifier._classifier_models() == ["deepseek/deepseek-v4-flash"]


def test_classifier_blocklist_can_be_overridden(monkeypatch):
    monkeypatch.setenv("HOA_CLASSIFIER_MODEL", "qwen/qwen3.5-flash-02-23")
    monkeypatch.delenv("HOA_CLASSIFIER_FALLBACK_MODEL", raising=False)
    monkeypatch.setenv("HOA_ALLOW_BLOCKLISTED_CLASSIFIER_MODELS", "1")

    assert doc_classifier._classifier_models() == ["qwen/qwen3.5-flash-02-23"]
