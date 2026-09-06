"""The generated .shortcut file has to be right without an iOS device to try it.

There is no iOS instrument in this homelab, so nothing here proves the Shortcuts
app will import the file. What these tests do prove is that every internal
reference is consistent, which is where a hand-rolled plist actually goes wrong:
a variable pointing at a UUID no action produces, an import question pointing at
the wrong action index, or a value that needed percent-encoding and did not get
it.
"""

import os
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


def test_both_values_are_percent_encoded_before_they_become_headers(shortcut):
    """The AA link carries :// ? and &, the title spaces and an apostrophe.

    Both now travel as HTTP headers, which cannot carry either raw.
    """
    actions = shortcut["WFWorkflowActions"]
    encoders = {
        a["WFWorkflowActionParameters"]["UUID"]
        for a in actions
        if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.urlencode"
    }
    assert len(encoders) == 2, "the url and the title both need encoding"

    request = actions[-1]["WFWorkflowActionParameters"]
    items = request["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
    by_name = {i["WFKey"]["Value"]["string"]: i["WFValue"] for i in items}

    assert by_name["X-Book-Url"]["Value"]["OutputUUID"] in encoders
    assert by_name["X-Book-Title"]["Value"]["OutputUUID"] in encoders


def test_the_request_posts_to_a_static_url_with_headers(shortcut):
    request = shortcut["WFWorkflowActions"][-1]
    assert request["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl"
    params = request["WFWorkflowActionParameters"]

    assert params["WFHTTPMethod"] == "POST"
    assert params["WFURL"] == ENDPOINT, "no variables embedded in the url"
    headers = params["WFHTTPHeaders"]
    assert headers["WFSerializationType"] == "WFDictionaryFieldValue"
    # The page is far too large for a header, so it stays the request body.
    assert params["WFHTTPBodyType"] == "File"


# --- the endpoint that hands it out ---------------------------------------


def test_shortcut_endpoint_serves_the_signed_file(monkeypatch):
    """Only a signed file installs.

    iOS 15 removed "Allow Untrusted Shortcuts" and an unsigned import was
    refused on a real phone on 2026-09-04. Apple's signed container is an
    Apple Encrypted Archive, so it starts AEA1 rather than being a readable
    plist.
    """
    monkeypatch.setattr(bs_main, "SHORTCUT_ICLOUD_URL", "")
    client = TestClient(bs_main.app)

    r = client.get("/shortcut")

    assert r.status_code == 200
    assert r.content[:4] == b"AEA1", "an unsigned shortcut cannot be installed"
    assert len(r.content) > len(open(bs_main.SHORTCUT_FILE, "rb").read())


def test_the_unsigned_file_is_kept_as_the_drift_reference(monkeypatch):
    """It cannot be served, but it is what the generator is checked against."""
    assert os.path.exists(bs_main.SHORTCUT_FILE)
    assert plistlib.loads(open(bs_main.SHORTCUT_FILE, "rb").read())[
        "WFWorkflowName"
    ] == "Download to Calibre"


def test_the_unsigned_file_is_served_only_if_no_signed_one_exists(monkeypatch, tmp_path):
    """A checkout nobody has signed yet should still hand out something."""
    monkeypatch.setattr(bs_main, "SHORTCUT_ICLOUD_URL", "")
    # A static dir holding the unsigned build and no signed sibling.
    (tmp_path / "download-to-calibre.shortcut").write_bytes(
        plistlib.dumps(build(), fmt=plistlib.FMT_BINARY)
    )
    monkeypatch.setattr(bs_main, "SHORTCUT_STATIC_DIR", str(tmp_path))
    client = TestClient(bs_main.app)

    r = client.get("/shortcut")

    assert r.status_code == 200
    assert plistlib.loads(r.content)["WFWorkflowName"] == "Download to Calibre"


def test_the_two_variants_differ_only_in_name_and_prompt():
    """Same actions, same endpoint. Only the label and the question change.

    Both Kindle addresses stay OUT of the published files: /shortcut is
    unauthenticated and a Kindle address is personal, so each is answered at
    install time instead of being baked in.
    """
    from tools.build_shortcut import VARIANTS

    mine = build(*VARIANTS[""])
    hers = build(*VARIANTS["anca"])

    assert mine["WFWorkflowName"] == "Download to Calibre"
    assert hers["WFWorkflowName"] == "Download to Calibre (Anca)"

    def shape(d):
        return [a["WFWorkflowActionIdentifier"] for a in d["WFWorkflowActions"]]

    assert shape(mine) == shape(hers), "the two must do the same thing"

    prompts = [q["Text"] for q in hers["WFWorkflowImportQuestions"]]
    assert any("Anca" in p for p in prompts), "hers should name whose Kindle it is"

    for d in (mine, hers):
        blob = plistlib.dumps(d, fmt=plistlib.FMT_BINARY)
        assert b"kindle.com" not in blob, "no Kindle address may be published"


def test_the_anca_variant_is_served_from_the_same_endpoint(monkeypatch):
    monkeypatch.setattr(bs_main, "SHORTCUT_ICLOUD_URL", "")
    client = TestClient(bs_main.app)

    mine = client.get("/shortcut")
    hers = client.get("/shortcut", params={"for": "anca"})

    assert mine.status_code == 200 and hers.status_code == 200
    assert mine.content[:4] == b"AEA1" and hers.content[:4] == b"AEA1"
    assert mine.content != hers.content, "they must be different shortcuts"


def test_an_unknown_variant_falls_back_rather_than_erroring(monkeypatch):
    monkeypatch.setattr(bs_main, "SHORTCUT_ICLOUD_URL", "")
    client = TestClient(bs_main.app)

    r = client.get("/shortcut", params={"for": "nobody"})

    assert r.status_code == 200
    assert r.content == client.get("/shortcut").content


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


@pytest.mark.parametrize("variant,stem", [
    ("", "download-to-calibre"),
    ("anca", "download-to-calibre-anca"),
])
def test_the_committed_files_match_the_generator(variant, stem):
    """Otherwise an edit to the generator ships nothing, or the reverse."""
    from tools.build_shortcut import VARIANTS

    static = os.path.join(os.path.dirname(bs_main.__file__), "static")
    with open(os.path.join(static, f"{stem}.shortcut"), "rb") as fh:
        committed = plistlib.load(fh)
    fresh = build(*VARIANTS[variant])

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
        f"re-run: tools/sign_shortcut.sh (or build_shortcut.py ... {variant})"
    )


@pytest.mark.parametrize("stem", ["download-to-calibre", "download-to-calibre-anca"])
def test_both_variants_ship_a_signed_copy(stem):
    """An unsigned shortcut cannot be installed on iOS 15 or later."""
    static = os.path.join(os.path.dirname(bs_main.__file__), "static")
    signed = os.path.join(static, f"{stem}.signed.shortcut")

    assert os.path.exists(signed), f"{stem} has no signed build; run tools/sign_shortcut.sh"
    with open(signed, "rb") as fh:
        assert fh.read(4) == b"AEA1"


def test_only_a_safari_web_page_is_accepted_as_input():
    """Running from Safari's share sheet on a book page failed on 2026-09-06:

        Get Details of Safari Web Page failed because Shortcuts couldn't
        convert from URL to Safari Web Page.

    Accepting WFURLContentItem alongside the page let the share sheet hand over
    the URL representation, and a URL cannot be converted back into a page.
    The URL and title coerce from a URL; page contents do not, and they are the
    one thing this flow exists to collect.
    """
    d = build()

    assert d["WFWorkflowInputContentItemClasses"] == ["WFSafariWebPageContentItem"]


def test_page_contents_is_read_before_anything_can_coerce_the_input():
    """It is the detail that cannot be recovered once the item is a URL."""
    d = build()
    safari = [
        a["WFWorkflowActionParameters"]["WFContentItemPropertyName"]
        for a in d["WFWorkflowActions"]
        if a["WFWorkflowActionIdentifier"].endswith("properties.safariwebpage")
    ]

    assert safari[0] == "Page Contents", f"read it first, got order {safari}"


def test_no_value_is_embedded_in_a_string(monkeypatch):
    """The construct that failed live on 2026-09-06.

    An inline attachment inside a WFTextTokenString, described by
    attachmentsByRange, resolved to nothing: the first real run posted url=,
    title= and kindle_email= all empty. A whole-value attachment inside a
    WFDictionaryFieldValue resolved fine, which is how the API key arrived. So
    the shortcut must carry every value the second way.
    """
    d = build()
    request = d["WFWorkflowActions"][-1]["WFWorkflowActionParameters"]

    assert isinstance(request["WFURL"], str), "the url must be a plain static string"
    assert request["WFURL"].startswith("https://")

    def has_inline_attachment(node):
        if isinstance(node, dict):
            if node.get("WFSerializationType") == "WFTextTokenString":
                if node["Value"].get("attachmentsByRange"):
                    return True
            return any(has_inline_attachment(v) for v in node.values())
        if isinstance(node, list):
            return any(has_inline_attachment(v) for v in node)
        return False

    assert not has_inline_attachment(d["WFWorkflowActions"]), (
        "no value may be embedded in a string; use a header instead"
    )


def test_every_value_the_server_needs_rides_in_a_header():
    d = build()
    request = d["WFWorkflowActions"][-1]["WFWorkflowActionParameters"]
    items = request["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
    names = [i["WFKey"]["Value"]["string"] for i in items]

    assert names == ["X-Api-Key", "X-Book-Url", "X-Book-Title", "X-Kindle-Email"]
    for item in items:
        assert item["WFValue"]["WFSerializationType"] == "WFTextTokenAttachment"
