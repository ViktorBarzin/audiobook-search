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
    assert questions, "at least the api key is asked for"

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


def test_no_value_passes_through_a_url_encode_action(shortcut):
    """URL Encode is the one step whose output never reached the server.

    Measured live 2026-09-06: the API key, the Kindle address and the page body
    all arrived, and each of those references an action output directly. The URL
    and the title arrived empty, and those two were the ones that went through
    is.workflow.actions.urlencode first. Headers carry raw values now, and the
    endpoint percent-decodes only what looks encoded.
    """
    ids = [a["WFWorkflowActionIdentifier"] for a in shortcut["WFWorkflowActions"]]
    assert "is.workflow.actions.urlencode" not in ids


def test_the_page_is_the_only_thing_read_off_safari(shortcut):
    """A URL and a title header were tried and both arrived empty.

    Only the first attachment in the header dictionary resolves, so carrying
    them was dead weight that also read as though it worked. The page holds the
    md5, the title and the author anyway.
    """
    reads = [a["WFWorkflowActionParameters"]["WFContentItemPropertyName"]
             for a in shortcut["WFWorkflowActions"]
             if a["WFWorkflowActionIdentifier"].endswith("properties.safariwebpage")]

    assert reads == ["Page Contents"]


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


def test_the_two_variants_differ_only_in_name_and_recipient():
    """Same actions, same endpoint. Only the label and X-Deliver-To change.

    Both Kindle addresses stay OUT of the published files: /shortcut is
    unauthenticated and an address is personal, so the file names a recipient
    and the server holds the address.
    """
    from tools.build_shortcut import VARIANTS

    mine = build(*VARIANTS[""])
    hers = build(*VARIANTS["anca"])

    assert mine["WFWorkflowName"] == "Download to Calibre"
    assert hers["WFWorkflowName"] == "Download to Calibre (Anca)"

    def shape(d):
        return [a["WFWorkflowActionIdentifier"] for a in d["WFWorkflowActions"]]

    assert shape(mine) == shape(hers), "the two must do the same thing"

    def recipient(d):
        items = (d["WFWorkflowActions"][-1]["WFWorkflowActionParameters"]
                 ["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"])
        by_name = {i["WFKey"]["Value"]["string"]: i for i in items}
        return by_name["X-Deliver-To"]["WFValue"]["Value"]["string"]

    assert recipient(mine) == "", "his imports and stops"
    assert recipient(hers) == "anca", "hers names whose Kindle it is"

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


def test_the_headers_are_the_api_key_and_the_recipient():
    """Nothing else travels. The page in the body carries the rest."""
    d = build()
    request = d["WFWorkflowActions"][-1]["WFWorkflowActionParameters"]
    items = request["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
    names = [i["WFKey"]["Value"]["string"] for i in items]

    assert names == ["X-Api-Key", "X-Deliver-To"]


def test_only_one_header_carries_a_variable(shortcut):
    """Everything but the first attachment in the dictionary arrived empty.

    Measured live 2026-09-07 with both shortcuts freshly installed: X-Api-Key
    is first and resolves, while X-Book-Url, X-Book-Title and X-Kindle-Email
    all came through empty. That held whether the value read a Safari page
    property or a Text action, so what decides is position, not kind. The page
    body is unaffected because it rides WFRequestVariable.
    """
    request = shortcut["WFWorkflowActions"][-1]["WFWorkflowActionParameters"]
    items = request["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]

    def is_attachment(item):
        return item["WFValue"].get("WFSerializationType") == "WFTextTokenAttachment"

    assert is_attachment(items[0]), "the first header is the one that resolves"
    assert not any(is_attachment(i) for i in items[1:]), (
        "a second attachment in this dictionary silently arrives empty"
    )


def test_the_recipient_is_a_plain_string(shortcut):
    request = shortcut["WFWorkflowActions"][-1]["WFWorkflowActionParameters"]
    items = request["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
    by_name = {i["WFKey"]["Value"]["string"]: i for i in items}

    value = by_name["X-Deliver-To"]["WFValue"]["Value"]
    assert value["string"] == ""
    assert not value["attachmentsByRange"], "nothing for iOS to resolve"


def test_the_anca_variant_names_her(shortcut):
    from tools.build_shortcut import VARIANTS, build

    anca = build(*VARIANTS["anca"])
    request = anca["WFWorkflowActions"][-1]["WFWorkflowActionParameters"]
    items = request["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
    by_name = {i["WFKey"]["Value"]["string"]: i for i in items}

    assert by_name["X-Deliver-To"]["WFValue"]["Value"]["string"] == "anca"
    assert "kindle.com" not in str(anca), (
        "the address stays on the server; /shortcut is served without auth"
    )


def test_there_is_only_one_import_question(shortcut):
    """The Kindle question could not work, so it is gone rather than misleading."""
    questions = shortcut["WFWorkflowImportQuestions"]

    assert len(questions) == 1
    assert "API key" in questions[0]["Text"]
