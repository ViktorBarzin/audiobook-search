"""The generated .shortcut file has to be right without an iOS device to try it.

There is no iOS instrument in this homelab, so nothing here proves the Shortcuts
app will import the file. What these tests do prove is that every internal
reference is consistent, which is where a hand-rolled plist actually goes wrong:
a variable pointing at a UUID no action produces, an import question pointing at
the wrong action index, or a value that needed percent-encoding and did not get
it.
"""

import plistlib

import pytest
from fastapi.testclient import TestClient

import backend.main as bs_main
from tools.build_shortcut import ENDPOINT, PLACEHOLDER, build


@pytest.fixture
def shortcut():
    return build()


def test_it_is_a_plist_that_round_trips(tmp_path, shortcut):
    path = tmp_path / "s.shortcut"
    path.write_bytes(plistlib.dumps(shortcut, fmt=plistlib.FMT_BINARY))
    assert plistlib.loads(path.read_bytes()) == shortcut


def test_it_shows_up_in_the_share_sheet_for_a_web_page(shortcut):
    assert shortcut["WFWorkflowTypes"] == ["ActionExtension"]
    assert "WFSafariWebPageContentItem" in shortcut["WFWorkflowInputContentItemClasses"]


def test_every_variable_points_at_an_action_that_produces_it(shortcut):
    """A dangling OutputUUID is the classic hand-rolled-plist bug."""
    actions = shortcut["WFWorkflowActions"]
    produced = {a["WFWorkflowActionParameters"]["UUID"] for a in actions}

    referenced = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("Type") == "ActionOutput":
                referenced.add(node["OutputUUID"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(actions)
    assert referenced, "the shortcut should wire some outputs together"
    assert referenced <= produced, f"dangling: {referenced - produced}"


def test_each_attachment_offset_lands_on_a_placeholder(shortcut):
    """attachmentsByRange keys must index the U+FFFC characters in the string."""
    def walk(node):
        if isinstance(node, dict):
            if node.get("WFSerializationType") == "WFTextTokenString":
                value = node["Value"]
                string = value["string"]
                for key in value["attachmentsByRange"]:
                    offset = int(key.strip("{}").split(",")[0])
                    assert string[offset] == PLACEHOLDER, (
                        f"offset {offset} of {string!r} is not a placeholder"
                    )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(shortcut["WFWorkflowActions"])


def test_the_import_questions_target_real_string_parameters(shortcut):
    """A question replaces a parameter's whole value, so it must be a string."""
    actions = shortcut["WFWorkflowActions"]
    questions = shortcut["WFWorkflowImportQuestions"]
    assert len(questions) == 2

    for q in questions:
        action = actions[q["ActionIndex"]]
        key = q["ParameterKey"]
        assert key in action["WFWorkflowActionParameters"], (
            f"question targets {key} which action {q['ActionIndex']} has no parameter for"
        )
        assert isinstance(action["WFWorkflowActionParameters"][key], str), (
            "an import question can only fill a plain string parameter"
        )
        assert q["Text"], "a question needs a prompt"


def test_no_secret_is_baked_into_the_published_file(shortcut):
    """The file is served unauthenticated, so it must carry no key."""
    blob = plistlib.dumps(shortcut, fmt=plistlib.FMT_BINARY)
    actions = shortcut["WFWorkflowActions"]
    for q in shortcut["WFWorkflowImportQuestions"]:
        assert actions[q["ActionIndex"]]["WFWorkflowActionParameters"][
            q["ParameterKey"]
        ] == "", "the key and Kindle address must be blank until import"
    assert b"X-Api-Key" in blob, "the header name itself is expected"


def test_both_values_are_percent_encoded_before_the_query_string(shortcut):
    """The AA link carries :// ? and &, the title carries spaces and an apostrophe."""
    actions = shortcut["WFWorkflowActions"]
    encoders = {
        a["WFWorkflowActionParameters"]["UUID"]
        for a in actions
        if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.urlencode"
    }
    assert len(encoders) == 2, "the url and the title both need encoding"

    request = actions[-1]["WFWorkflowActionParameters"]
    url_token = request["WFURL"]["Value"]
    string = url_token["string"]
    assert string.startswith(ENDPOINT + "?url=")

    # The attachments sitting in ?url= and &title= must be the encoders' output.
    used = [
        att["OutputUUID"]
        for key, att in sorted(
            url_token["attachmentsByRange"].items(),
            key=lambda kv: int(kv[0].strip("{}").split(",")[0]),
        )
    ]
    assert used[0] in encoders, "the shared link must go through URL Encode"
    assert used[1] in encoders, "the title must go through URL Encode"


def test_the_request_posts_with_the_key_in_a_header(shortcut):
    request = shortcut["WFWorkflowActions"][-1]
    assert request["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl"
    params = request["WFWorkflowActionParameters"]
    assert params["WFHTTPMethod"] == "POST"
    headers = params["WFHTTPHeaders"]
    assert headers["WFSerializationType"] == "WFDictionaryFieldValue"
    items = headers["Value"]["WFDictionaryFieldValueItems"]
    assert [i["WFKey"]["Value"]["string"] for i in items] == ["X-Api-Key"]
    # In a header, not the query string, so it stays out of access logs.
    assert "key=" not in params["WFURL"]["Value"]["string"]


# --- the endpoint that hands it out ---------------------------------------


def test_shortcut_endpoint_serves_the_file_when_no_icloud_url(monkeypatch):
    monkeypatch.setattr(bs_main, "SHORTCUT_ICLOUD_URL", "")
    client = TestClient(bs_main.app)

    r = client.get("/shortcut")

    assert r.status_code == 200
    assert plistlib.loads(r.content)["WFWorkflowName"] == "Download to Calibre"


def test_an_icloud_url_still_wins(monkeypatch):
    monkeypatch.setattr(bs_main, "SHORTCUT_ICLOUD_URL", "https://www.icloud.com/shortcuts/abc")
    client = TestClient(bs_main.app)

    r = client.get("/shortcut", follow_redirects=False)

    assert r.status_code in (302, 307)
    assert r.headers["location"] == "https://www.icloud.com/shortcuts/abc"


def test_the_endpoint_needs_no_api_key(monkeypatch):
    """The Shortcuts app fetches this with no credentials."""
    monkeypatch.setattr(bs_main, "SHORTCUT_ICLOUD_URL", "")
    monkeypatch.setattr(bs_main, "API_KEY", "test-key")
    client = TestClient(bs_main.app)

    assert client.get("/shortcut").status_code == 200


def test_the_committed_file_matches_the_generator():
    """Otherwise an edit to the generator ships nothing, or the reverse."""
    with open(bs_main.SHORTCUT_FILE, "rb") as fh:
        committed = plistlib.load(fh)
    fresh = build()

    # UUIDs are regenerated on every build, so compare shape without them.
    def strip_uuids(node):
        if isinstance(node, dict):
            return {
                k: strip_uuids(v)
                for k, v in node.items()
                if k not in ("UUID", "OutputUUID")
            }
        if isinstance(node, list):
            return [strip_uuids(v) for v in node]
        return node

    assert strip_uuids(committed) == strip_uuids(fresh), (
        "re-run: python3 tools/build_shortcut.py "
        "backend/static/download-to-calibre.shortcut"
    )
