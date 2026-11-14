import base64
import json

import httpx
import pytest
from anthropic import Anthropic

from prompt_studio.agents import ClaudeRewriter
from prompt_studio.errors import GenerationError
from prompt_studio.models import DocumentResponse, EditResponse, ReviewResponse
from prompt_studio.pdf_source import read_pdf
from prompt_studio.settings import Settings


@pytest.mark.parametrize("stage", ["rewrite", "corrective_rewrite", "review"])
def test_anthropic_sdk_sends_original_pdf_properties_and_image(stage, pdf, character, png):
    source = read_pdf(pdf)
    output = DocumentResponse(content="A complete adapted document in the source format.")
    review = ReviewResponse(passed=True, issues=[])

    def handle(request):
        body = json.loads(request.content)
        content = body["messages"][0]["content"]
        payload = json.loads(content[0]["text"])
        assert payload["saved_character"] == character.model_dump()
        assert payload["attachment_instruction"] == "ATTACHMENT: Attach maya-cake.png as @image_1."
        assert "source" not in payload
        assert content[1]["type"] == "document"
        assert content[1]["source"]["media_type"] == "application/pdf"
        assert base64.b64decode(content[1]["source"]["data"]) == pdf
        assert base64.b64decode(content[2]["source"]["data"]) == png
        if stage == "review":
            assert payload["result"] == output.model_dump()
            response = review
        else:
            assert "content" in body["output_config"]["format"]["schema"]["properties"]
            assert payload["correction_feedback"] == (
                None if stage == "rewrite" else {"issues": []}
            )
            response = output
        return httpx.Response(
            200,
            json={
                "id": "msg_pdf",
                "type": "message",
                "role": "assistant",
                "model": Settings.claude_model,
                "content": [{"type": "text", "text": response.model_dump_json()}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 20, "output_tokens": 30},
            },
        )

    with Anthropic(
        api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
    ) as client:
        agent = ClaudeRewriter(Settings(), client=client)
        kwargs = {"pdf": pdf, "png_filename": "maya-cake.png"}
        if stage == "review":
            assert agent.review(source, output, character, png, **kwargs).passed
        else:
            feedback = None if stage == "rewrite" else {"issues": []}
            assert agent.rewrite(source, character, png, feedback, **kwargs) == output


def test_document_agent_requires_original_pdf(pdf, character, png):
    agent = ClaudeRewriter(Settings(), client=object())
    with pytest.raises(GenerationError, match="original PDF"):
        agent.rewrite(read_pdf(pdf), character, png)


def test_anthropic_sdk_serializes_and_parses_targeted_edits(source, character, png):
    def handle(request):
        body = json.loads(request.content)
        assert body["output_config"]["format"]["type"] == "json_schema"
        assert "edits" in body["output_config"]["format"]["schema"]["properties"]
        assert body["messages"][0]["content"][1]["source"]["media_type"] == "image/png"
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": Settings.claude_model,
                "content": [{"type": "text", "text": EditResponse(edits=[]).model_dump_json()}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 20, "output_tokens": 30},
            },
            headers={"request-id": "request_test"},
        )

    with Anthropic(
        api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
    ) as client:
        agent = ClaudeRewriter(Settings(), client=client)
        assert agent.rewrite(source, character, png).edits == []
        assert agent.usage[0]["tokens"]["input_tokens"] == 20
        assert agent.usage[0]["request_id"] == "request_test"


def test_provider_exception_bodies_are_not_exposed(source, character, png, caplog):
    def handle(request):
        return httpx.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "SECRET_PROVIDER_BODY"},
            },
            headers={"request-id": "req_rejected"},
        )

    with Anthropic(
        api_key="test-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        agent = ClaudeRewriter(Settings(), client=client)
        with pytest.raises(GenerationError) as error:
            agent.rewrite(source, character, png)
    assert "SECRET_PROVIDER_BODY" not in str(error.value)
    assert "SECRET_PROVIDER_BODY" not in caplog.text
    assert "req_rejected" in caplog.text


def test_real_google_sdk_builds_reserved_id_multipart_upload():
    import httplib2
    from googleapiclient.discovery import build

    from prompt_studio.storage import APP, FOLDER, DriveStorage

    class LocalHTTP:
        def __init__(self):
            self.requests = []

        def request(self, uri, method="GET", body=None, headers=None, **kwargs):
            self.requests.append((uri, method, body, headers))
            if "generateIds" in uri:
                result = {"ids": ["reserved-root"]}
            elif (
                uri.endswith(
                    "/reserved-root?fields=id%2Cname%2CmimeType%2Cparents%2CappProperties%2CwebViewLink&alt=json"
                )
                or "/files/reserved-root?" in uri
            ):
                result = {
                    "id": "reserved-root",
                    "name": "Studio",
                    "mimeType": FOLDER,
                    "appProperties": {"studio": APP, "kind": "root"},
                    "parents": [],
                }
            elif "/files/upload-id?" in uri:
                return httplib2.Response(
                    {"status": "404", "content-type": "application/json"}
                ), b'{"error":{"code":404,"message":"missing"}}'
            elif method == "POST" and "uploadType=multipart" in uri:
                assert isinstance(body, bytes)
                assert b'"id": "upload-id"' in body
                assert b'"parents": ["reserved-root"]' in body
                assert b'"studio": "character-prompt-studio"' in body
                assert b'"permissions"' not in body
                assert b"checkpoint-payload" in body
                result = {"id": "upload-id"}
            elif method == "GET":
                result = {"files": []}
            else:
                result = {"id": "reserved-root"}
            return httplib2.Response(
                {"status": "200", "content-type": "application/json"}
            ), json.dumps(result).encode()

    transport = LocalHTTP()
    service = build("drive", "v3", http=transport, cache_discovery=False, static_discovery=True)
    storage = DriveStorage(Settings(drive_root_id="reserved-root"), service=service)
    storage.put(
        "upload-id", "checkpoint.json", "reserved-root", b"checkpoint-payload", "application/json"
    )
    assert any(
        "uploadType=multipart" in uri and method == "POST"
        for uri, method, body, headers in transport.requests
    )
