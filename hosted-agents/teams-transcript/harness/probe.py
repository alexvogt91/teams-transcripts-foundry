"""Read-only candidate probe before endpoint promotion; never print transcript data."""

import json
import urllib.request

from deploy import AGENT_NAME, project_endpoint


def probe(version, access_token, *, expect_read=True):
    body = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Call list_transcripts(page=0) once. Return only TEAMS_CORPUS_READ_OK if the tool succeeds, otherwise TEAMS_CORPUS_UNAVAILABLE. Do not include transcript text or identifiers.",
                    }
                ],
            },
        ],
        "agent_reference": {
            "type": "agent_reference",
            "name": AGENT_NAME,
            "version": str(version),
        },
        "stream": True,
        "store": False,
    }
    req = urllib.request.Request(
        f"{project_endpoint()}/agents/{AGENT_NAME}/endpoint/protocols/openai/v1/responses?api-version=v1",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    text, called, completed = "", False, False
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                event = json.loads(line[5:])
                kind = event.get("type")
                item = event.get("item") or {}
                if item.get("name") == "list_transcripts":
                    called = True
                if kind == "response.output_text.delta":
                    text += event.get("delta", "")
                if kind in ("response.failed", "response.incomplete", "error"):
                    raise ValueError
                if kind == "response.completed":
                    completed = True
                    final = event.get("response", {})
                    served = (final.get("agent_reference") or {}).get("version")
                    served = (
                        served or final.get("agent_version") or final.get("version")
                    )
                    if str(served) != str(version):
                        raise ValueError
    except Exception:
        raise SystemExit(
            "Teams candidate probe unavailable; endpoint not promoted"
        ) from None
    marker = "TEAMS_CORPUS_READ_OK" if expect_read else "TEAMS_CORPUS_UNAVAILABLE"
    if not completed or not called or text.strip() != marker:
        expected = "delegated corpus read" if expect_read else "application-only denial"
        raise SystemExit(
            f"Teams candidate cannot prove {expected}; endpoint not promoted"
        )
    print(
        "Pinned Teams candidate verified "
        + (
            "delegated corpus access"
            if expect_read
            else "application-only corpus denial"
        )
    )


if __name__ == "__main__":
    import sys
    from deploy import token

    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit(
            "Usage: probe.py <candidate-version> (uses your signed-in Azure CLI user)"
        )
    probe(sys.argv[1], token())
