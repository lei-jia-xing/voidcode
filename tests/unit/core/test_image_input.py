"""End-to-end unit tests for image attachment input, persistence, recovery, and provider serialization."""

from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.capabilities.providers.native.anthropic_native import AnthropicMessagesProvider
from voidcode.capabilities.providers.native.google_native import GoogleGenAIProvider
from voidcode.capabilities.providers.native.openai_native import OpenAIChatCompletionsProvider
from voidcode.core.events import EventType
from voidcode.core.storage.store import EventDraft, SessionStatus, SqliteStore
from voidcode.core.transcript import transcript_entry
from voidcode.sdk.client import RunSpec
from voidcode.sdk.provider import (
    ImageAttachment,
    ProviderExecutionError,
    ProviderMessage,
    ProviderTurnRequest,
)
from voidcode.sdk.transcript import decode_transcript_content, encode_transcript_content
from voidcode.tui.rendering import content_rows as _content_rows
from voidcode.tui.styles import Styles


def test_image_attachment_contract() -> None:
    img = ImageAttachment(media_type="image/png", data="aGVsbG8=", artifact_id="art-1")
    assert img.media_type == "image/png"
    assert img.data == "aGVsbG8="
    assert img.artifact_id == "art-1"

    payload = img.as_payload()
    assert payload == {"media_type": "image/png", "data": "aGVsbG8=", "artifact_id": "art-1"}

    restored = ImageAttachment.from_payload(payload)
    assert restored == img

    with pytest.raises(ValueError, match="media_type"):
        ImageAttachment(media_type="", data="abc")
    with pytest.raises(ValueError, match="data"):
        ImageAttachment(media_type="image/png", data="")


def test_run_spec_and_provider_message_validation() -> None:
    img = ImageAttachment(media_type="image/jpeg", data="MTIz")
    spec = RunSpec(prompt="describe this image", images=(img,))
    assert spec.images == (img,)

    msg = ProviderMessage(role="user", content="hello", images=(img,))
    assert msg.images == (img,)

    with pytest.raises(ValueError, match="ImageAttachment"):
        RunSpec(prompt="test", images=("not-an-image",))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ImageAttachment"):
        ProviderMessage(role="user", content="test", images=("not-an-image",))  # type: ignore[arg-type]


def test_transcript_wire_round_trip() -> None:
    img = ImageAttachment(media_type="image/webp", data="d2VicA==")
    msg = ProviderMessage(role="user", content="what is this?", images=(img,))

    encoded = encode_transcript_content(msg)
    assert encoded["kind"] == "message"

    decoded = decode_transcript_content(encoded)
    assert isinstance(decoded, ProviderMessage)
    assert decoded.role == "user"
    assert decoded.content == "what is this?"
    assert len(decoded.images) == 1
    assert decoded.images[0].media_type == "image/webp"
    assert decoded.images[0].data == "d2VicA=="


def test_event_persistence_and_session_recovery_preserves_images(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    with SqliteStore(db_path) as store:
        rev_id = store.put_revision({"plugins": []})
        store.record_workspace_revision("/ws", rev_id)
        store.create_session("sess-1", "/ws", rev_id)

        img1 = ImageAttachment(media_type="image/png", data="Zmlyc3Q=", artifact_id="art-1")
        img2 = ImageAttachment(media_type="image/gif", data="c2Vjb25k")

        event_payload = {
            "content": "check these two images",
            "images": [img1.as_payload(), img2.as_payload()],
        }
        draft = EventDraft(EventType.USER_MESSAGE, "client", "user", event_payload)
        store.commit_staged(
            "sess-1",
            expected_sequence=0,
            expected_run_id=None,
            run_id="run-1",
            revision_id=rev_id,
            events=(draft,),
            status=SessionStatus.RUNNING,
        )

        events = store.events("sess-1")
        assert len(events) == 1
        user_event = events[0]
        assert user_event.event_type == EventType.USER_MESSAGE
        assert [dict(img) for img in user_event.payload["images"]] == [img1.as_payload(), img2.as_payload()]

        # Transcript projection preserves images
        entry = transcript_entry(user_event)
        assert entry is not None
        assert isinstance(entry.content, ProviderMessage)
        assert entry.content.role == "user"
        assert entry.content.content == "check these two images"
        assert entry.content.images == (img1, img2)

        # Session recovery reconstructing ProviderMessage does not duplicate or drop attachments
        entry_roundtrip = transcript_entry(user_event)
        assert entry_roundtrip is not None
        assert isinstance(entry_roundtrip.content, ProviderMessage)
        assert entry_roundtrip.content.images == (img1, img2)


def test_followup_queue_retains_images_without_loss(tmp_path: Path) -> None:
    db_path = tmp_path / "test_followup.sqlite3"
    with SqliteStore(db_path) as store:
        rev_id = store.put_revision({"plugins": []})
        store.record_workspace_revision("/ws", rev_id)
        store.create_session("sess-followup", "/ws", rev_id)

        img = ImageAttachment(media_type="image/png", data="cG5nZGF0YQ==")
        continuation_payload = {
            "prompt": "followup with image",
            "images": [img.as_payload()],
            "max_steps": 16,
            "root_action_budget": None,
        }
        store.enqueue_continuation(
            "cid-1",
            "core",
            "sess-followup",
            "user_message",
            "run-1",
            rev_id,
            continuation_payload,
            root_id="root-followup",
            role="user",
        )

        claimed = store.claim_pending_followup("sess-followup")
        assert claimed is not None
        cid, prompt, max_steps, raw_images, root_id, root_budget = claimed
        assert cid == "cid-1"
        assert prompt == "followup with image"
        assert max_steps == 16
        assert root_id == "root-followup"
        assert root_budget is None
        assert len(raw_images) == 1
        reconstructed_img = ImageAttachment.from_payload(raw_images[0])
        assert reconstructed_img == img


def test_anthropic_native_serialization() -> None:
    provider = AnthropicMessagesProvider()
    img = ImageAttachment(media_type="image/png", data="YW50aHJvcGlj")
    req = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="analyze this", images=(img,)),),
        model_name="claude-sonnet-4-5",
        provider_name="anthropic",
    )
    payload = provider._payload(req, stream=False)
    messages = payload["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "analyze this"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "YW50aHJvcGlj"},
    }


def test_openai_native_serialization() -> None:
    provider = OpenAIChatCompletionsProvider()
    img = ImageAttachment(media_type="image/jpeg", data="b3BlbmFp")
    # aiand/deepseek-ai/deepseek-v4.1-flash has supports_vision: true
    req_vision = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="what is here?", images=(img,)),),
        model_name="deepseek-ai/deepseek-v4.1-flash",
        provider_name="aiand",
    )
    payload = provider._payload(req_vision, stream=False)
    messages = payload["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content == [
        {"type": "text", "text": "what is here?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,b3BlbmFp"}},
    ]


def test_google_native_serialization() -> None:
    provider = GoogleGenAIProvider()
    img = ImageAttachment(media_type="image/webp", data="Z29vZ2xl")
    req = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="look at photo", images=(img,)),),
        model_name="gemini-2.5-pro",
        provider_name="google",
    )
    _system, contents = provider._contents(req)
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    parts = contents[0]["parts"]
    assert isinstance(parts, list)
    assert parts == [
        {"text": "look at photo"},
        {"inline_data": {"mime_type": "image/webp", "data": "Z29vZ2xl"}},
    ]


def test_rejection_on_unsupported_text_only_models() -> None:
    img = ImageAttachment(media_type="image/png", data="ZGF0YQ==")
    # deepseek-v4-pro in static catalog has supports_vision: false
    openai_provider = OpenAIChatCompletionsProvider()
    req = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="see image", images=(img,)),),
        model_name="deepseek-v4-pro",
        provider_name="deepseek",
    )
    with pytest.raises(ProviderExecutionError) as excinfo:
        openai_provider._payload(req, stream=False)
    assert excinfo.value.kind == "unsupported_feature"
    assert "does not support image input" in excinfo.value.message

    # Anthropic provider rejecting unsupported vision model
    anthropic_provider = AnthropicMessagesProvider()
    with pytest.raises(ProviderExecutionError) as excinfo_ant:
        anthropic_provider._payload(req, stream=False)
    assert excinfo_ant.value.kind == "unsupported_feature"
    assert "does not support image input" in excinfo_ant.value.message

    # Google provider rejecting unsupported vision model
    google_provider = GoogleGenAIProvider()
    with pytest.raises(ProviderExecutionError) as excinfo_goog:
        google_provider._contents(req)
    assert excinfo_goog.value.kind == "unsupported_feature"
    assert "does not support image input" in excinfo_goog.value.message


def test_tui_content_rows_renders_image_badge() -> None:
    img = ImageAttachment(media_type="image/png", data="ZGF0YQ==")
    msg = ProviderMessage(role="user", content="explain this chart", images=(img,))
    rows = _content_rows(msg, width=80, styles=Styles(None))
    assert any("[Image: image/png]" in row for row in rows)
    assert any("explain this chart" in row for row in rows)
