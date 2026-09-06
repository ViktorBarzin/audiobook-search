#!/usr/bin/env python3
"""Build the "Download to Calibre" iOS Shortcut as an unsigned .shortcut file.

Why generated rather than hand-built: the flow needs the AA page TITLE, because
Anna's Archive is human-only for us (DDoS-Guard 403s /md5/ for plain requests,
for six real browser TLS handshakes via curl-impersonate, and for the cluster's
headful Chrome). The phone can read AA, so the phone sends the title.

Why UNSIGNED: from iOS 15 a .shortcut file is signed, and `shortcuts sign` is
a macOS-only tool. There is no Mac here, so this file needs "Allow Untrusted
Shortcuts" enabled once (Settings -> Shortcuts). The eventual upgrade is an
Apple-signed iCloud link: share this shortcut from the phone once and point
SHORTCUT_ICLOUD_URL at it, and /shortcut redirects there instead of serving
this file.

Why query parameters instead of a JSON body: the shortcut file format's
WFTextTokenString is well documented, whereas the JSON-body parameter is named
inconsistently across references (WFJSONBody in one action library,
WFJSONValues in another) and the wrong name sends an empty body with no error.
There is no iOS instrument in this homelab, so the file has to be right without
being testable. A URL carrying query params is the simplest correct thing.
/api/download-url reads query params for exactly this reason.

Format reference: github.com/sebj/iOS-Shortcuts-Reference

Run:  python3 tools/build_shortcut.py backend/static/download-to-calibre.shortcut
"""

import plistlib
import sys
import uuid

# Shortcuts marks a variable's position in a string with U+FFFC (OBJECT
# REPLACEMENT CHARACTER) and describes it in attachmentsByRange, keyed by
# "{offset, 1}" against that character's index.
PLACEHOLDER = "￼"

ENDPOINT = "https://book-search.viktorbarzin.me/api/download-url"

# "Show in Share Sheet", so the shortcut appears when sharing from Safari.
WORKFLOW_TYPES = ["ActionExtension"]
# ONLY the Safari web page. Accepting WFURLContentItem alongside it made
# Safari's share sheet hand over the URL representation instead, and Shortcuts
# cannot convert a URL back into a page: running it from the share sheet on a
# book page failed with "Get Details of Safari Web Page failed because
# Shortcuts couldn't convert from URL to Safari Web Page" (2026-09-06). The URL
# and title coerce from a URL, but page contents genuinely need a rendered
# page, which is the one thing this flow cannot do without.
INPUT_CLASSES = ["WFSafariWebPageContentItem"]


def new_uuid() -> str:
    """Uppercase, which is what the app itself emits."""
    return str(uuid.uuid4()).upper()


def action_output(output_uuid: str, output_name: str) -> dict:
    """A reference to a previous action's output."""
    return {
        "Value": {
            "OutputUUID": output_uuid,
            "OutputName": output_name,
            "Type": "ActionOutput",
        },
        "WFSerializationType": "WFTextTokenAttachment",
    }


def extension_input() -> dict:
    """A reference to whatever the share sheet handed the shortcut."""
    return {
        "Value": {"Type": "ExtensionInput", "Aggrandizements": []},
        "WFSerializationType": "WFTextTokenAttachment",
    }


def text_token(parts: list) -> dict:
    """Build a WFTextTokenString from literal strings and attachment dicts.

    Each attachment contributes one U+FFFC to the string and one entry in
    attachmentsByRange at that character's offset.
    """
    string = ""
    attachments = {}
    for part in parts:
        if isinstance(part, str):
            string += part
        else:
            attachments[f"{{{len(string)}, 1}}"] = part["Value"]
            string += PLACEHOLDER
    return {
        "Value": {"string": string, "attachmentsByRange": attachments},
        "WFSerializationType": "WFTextTokenString",
    }


def dictionary_value(items: list[tuple[str, dict]]) -> dict:
    """A WFDictionaryFieldValue of text keys to text values.

    WFItemType 0 is Text (1 dictionary, 2 array, 3 number, 4 boolean).
    """
    return {
        "Value": {
            "WFDictionaryFieldValueItems": [
                {
                    "WFItemType": 0,
                    "WFKey": text_token([key]),
                    "WFValue": value,
                }
                for key, value in items
            ]
        },
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def safari_property(prop: str, out_uuid: str) -> dict:
    """Read one detail off the shared Safari web page."""
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.properties.safariwebpage",
        "WFWorkflowActionParameters": {
            "UUID": out_uuid,
            "WFInput": extension_input(),
            "WFContentItemPropertyName": prop,
        },
    }


def text_action(out_uuid: str, value: str = "") -> dict:
    """A plain Text action, used to hold a value an import question fills in.

    An import question replaces a parameter's whole value with the user's
    plain-text answer, so it can only sensibly fill a STRING parameter. The API
    key belongs in a header, whose value is a nested dictionary structure, so
    the question fills this action's WFTextActionText instead and the header
    references this action's output.
    """
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
        "WFWorkflowActionParameters": {
            "UUID": out_uuid,
            "WFTextActionText": value,
        },
    }


def build(name: str = "Download to Calibre", who: str = "you") -> dict:
    key_uuid = new_uuid()
    mail_uuid = new_uuid()
    url_uuid = new_uuid()
    title_uuid = new_uuid()

    page_uuid = new_uuid()

    actions = [
        text_action(key_uuid),
        text_action(mail_uuid),
        # Page contents FIRST: it is the detail that cannot be recovered from a
        # URL, so it is read before any other action has a chance to coerce the
        # shared item.
        safari_property("Page Contents", page_uuid),
        safari_property("URL", url_uuid),
        safari_property("Name", title_uuid),
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
            "WFWorkflowActionParameters": {
                "UUID": new_uuid(),
                "WFHTTPMethod": "POST",
                # A STATIC url. Embedding variables in a string did not
                # work: the first real run on 2026-09-06 posted url=, title=
                # and kindle_email= all empty, four times over, while the API
                # key in a header arrived fine. An inline attachment described
                # by attachmentsByRange resolved to nothing; a whole-value
                # attachment inside a dictionary resolved correctly. So every
                # value now rides in a header, which is the shape that works.
                "WFURL": ENDPOINT,
                # Raw, straight off the shared page. Percent-encoding these
                # through a URL Encode action is what broke the second live run
                # on 2026-09-06: the api key, the Kindle address and the page
                # body all arrived, and those three reference an action output
                # directly, while the url and the title went through
                # is.workflow.actions.urlencode and arrived empty. The endpoint
                # decodes only values that look encoded, so both shapes work.
                "WFHTTPHeaders": dictionary_value([
                    ("X-Api-Key", action_output(key_uuid, "Text")),
                    ("X-Book-Url", action_output(url_uuid, "URL")),
                    ("X-Book-Title", action_output(title_uuid, "Name")),
                    ("X-Kindle-Email", action_output(mail_uuid, "Text")),
                ]),
                "WFHTTPBodyType": "File",
                "WFRequestVariable": action_output(page_uuid, "Page Contents"),
            },
        },
    ]

    return {
        "WFWorkflowClientVersion": "1200.3",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowName": name,
        "WFWorkflowTypes": WORKFLOW_TYPES,
        "WFWorkflowInputContentItemClasses": INPUT_CLASSES,
        "WFWorkflowIcon": {
            # Blue, book glyph.
            "WFWorkflowIconStartColor": 463140863,
            "WFWorkflowIconGlyphNumber": 59473,
        },
        "WFWorkflowImportQuestions": [
            {
                "ActionIndex": 0,
                "Category": "Parameter",
                "ParameterKey": "WFTextActionText",
                "Text": "Paste your book-search API key",
                "DefaultValue": "",
            },
            {
                "ActionIndex": 1,
                "Category": "Parameter",
                "ParameterKey": "WFTextActionText",
                "Text": f"Kindle address for {who} (leave blank to skip emailing)",
                "DefaultValue": "",
            },
        ],
        "WFWorkflowActions": actions,
    }


# The two shortcuts differ only in their name and in whose Kindle address the
# import question asks for. Both addresses stay OUT of the published files:
# /shortcut is unauthenticated, and a Kindle address is personal, so each is
# filled in at install time instead of being baked in.
VARIANTS = {
    "": ("Download to Calibre", "you"),
    "anca": ("Download to Calibre (Anca)", "Anca"),
}


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("usage: build_shortcut.py <out.shortcut> [variant]", file=sys.stderr)
        return 2
    out = sys.argv[1]
    variant = sys.argv[2] if len(sys.argv) == 3 else ""
    if variant not in VARIANTS:
        print(f"unknown variant {variant!r}, want one of {sorted(VARIANTS)}", file=sys.stderr)
        return 2
    name, who = VARIANTS[variant]
    with open(out, "wb") as fh:
        plistlib.dump(build(name, who), fh, fmt=plistlib.FMT_BINARY)
    print(f"wrote {out} ({name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
