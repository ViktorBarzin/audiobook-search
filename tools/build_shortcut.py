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

# "Show in Share Sheet", so the shortcut appears when sharing from Safari or Photos.
WORKFLOW_TYPES = ["ActionExtension"]
# ONLY the Safari web page. Accepting WFURLContentItem alongside it made
# Safari's share sheet hand over the URL representation instead, and Shortcuts
# cannot convert a URL back into a page: running it from the share sheet on a
# book page failed with "Get Details of Safari Web Page failed because
# Shortcuts couldn't convert from URL to Safari Web Page" (2026-09-06). The URL
# and title coerce from a URL, but page contents genuinely need a rendered
# page, which is the one thing this flow cannot do without.
# A photo shared from Photos arrives as an image. Whether accepting images
# changes what Safari hands over, as accepting URLs did, is checked on the
# iPhone before this ships (docs/2026-10-29-book-from-a-photo.md).
INPUT_CLASSES = ["WFSafariWebPageContentItem", "WFImageContentItem"]


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


WAIT_ENDPOINT = "https://book-search.viktorbarzin.me/api/download-status/wait"
# Six asks of up to 20 s each: iOS gives up on one request at about 25 s, and
# the phone waits about two minutes in all before the answer moves to Slack.
WAITS_IN_LOOP = 5
# A JPEG of this size is plenty for Claude to read a cover, and small enough
# to upload quickly from a phone. The server shrinks it further either way.
PHOTO_LONGEST_EDGE = "1600"


def variable_input(attachment: dict) -> dict:
    """The WFInput of an If action: a variable, not a text string."""
    return {"Type": "Variable", "Variable": attachment}


def control(identifier: str, group: str, mode: int, **params) -> dict:
    """One part of a block: 0 opens it, 1 is Otherwise or a menu case, 2 ends it."""
    return {
        "WFWorkflowActionIdentifier": identifier,
        "WFWorkflowActionParameters": {"GroupingIdentifier": group,
                                       "WFControlFlowMode": mode, **params},
    }


def action(identifier: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": identifier, "WFWorkflowActionParameters": params}


def post(body: dict, out_uuid: str, key_uuid: str, deliver_to: str) -> dict:
    """POST a body to the share endpoint.

    A STATIC url. Embedding variables in a string did not work: the first real
    run on 2026-09-06 posted url=, title= and kindle_email= all empty, four
    times over, while the API key in a header arrived fine. An inline
    attachment described by attachmentsByRange resolved to nothing; a
    whole-value attachment inside a dictionary resolved correctly.

    ONE attachment in the headers, and it is first. Everything after the first
    item in the dictionary arrives empty, whatever it references (measured
    live 2026-09-07 with both shortcuts installed fresh). So who to email is a
    literal string naming a recipient and the server holds the addresses,
    which also keeps them out of this file, since /shortcut is served without
    authentication.
    """
    return action(
        "is.workflow.actions.downloadurl",
        UUID=out_uuid,
        WFHTTPMethod="POST",
        WFURL=ENDPOINT,
        WFHTTPHeaders=dictionary_value([
            ("X-Api-Key", action_output(key_uuid, "Text")),
            ("X-Deliver-To", text_token([deliver_to])),
        ]),
        WFHTTPBodyType="File",
        WFRequestVariable=body,
    )


def wait(out_uuid: str, job_uuid: str, last: bool = False) -> dict:
    """Ask the wait endpoint for the share's answer. The job id rides in the
    first header, the one position whose variable resolves."""
    headers = [("X-Job-Id", action_output(job_uuid, "Dictionary Value"))]
    if last:
        headers.append(("X-Last-Wait", text_token(["1"])))
    return action(
        "is.workflow.actions.downloadurl",
        UUID=out_uuid,
        WFHTTPMethod="GET",
        WFURL=WAIT_ENDPOINT,
        WFHTTPHeaders=dictionary_value(headers),
    )


def show(attachment: dict) -> dict:
    """Show the answer with Quick Look, which takes the variable whole.

    Show Result would embed it in a text string, and an embedded variable is
    the construct that arrived empty on 2026-09-06.
    """
    return action("is.workflow.actions.previewdocument", WFInput=attachment)


def build(name: str = "Download to Calibre", deliver_to: str = "") -> dict:
    """One shortcut for a shared Anna's Archive page and for a photo of a cover.

    Shared from Safari, it posts the rendered page, as it always has. Shared
    from Photos, it posts the photo as a JPEG. Run from the home screen with
    nothing shared, it asks for a new photo or one from the library. Either way
    it then asks the wait endpoint six times and shows the last answer: the
    line the share ended with, or where its result will land.
    """
    key_uuid, page_uuid, page_reply = new_uuid(), new_uuid(), new_uuid()
    photo_reply, reply_if, photo_if = new_uuid(), new_uuid(), new_uuid()
    menu_uuid, given_uuid, resized, jpeg = new_uuid(), new_uuid(), new_uuid(), new_uuid()
    job_uuid, last_uuid, type_uuid = new_uuid(), new_uuid(), new_uuid()
    by_kind, has_input, menu, repeat = new_uuid(), new_uuid(), new_uuid(), new_uuid()
    take, choose = "Take a photo", "Choose from library"

    actions = [
        text_action(key_uuid),
        # What was shared: "Safari web page", "Photo media", "Image", or
        # nothing. Coercing the input to a page instead worked for nothing
        # and threw for a photo from Photos ("couldn't convert from Photo media
        # to Safari Web Page", rig, 2026-10-30).
        action("is.workflow.actions.getitemtype", UUID=type_uuid, WFInput=extension_input()),
        control("is.workflow.actions.conditional", by_kind, 0,
                WFInput=variable_input(action_output(type_uuid, "Type")),
                WFCondition=99, WFConditionalActionString=text_token(["Safari"])),
        # The page is all the server needs: Anna's Archive names its own md5
        # in every download link on it, and the title and author are there too.
        safari_property("Page Contents", page_uuid),
        post(action_output(page_uuid, "Page Contents"), page_reply, key_uuid, deliver_to),
        control("is.workflow.actions.conditional", by_kind, 1),
        control("is.workflow.actions.conditional", has_input, 0,
                WFInput=variable_input(extension_input()), WFCondition=100),
        action("is.workflow.actions.getvariable", UUID=given_uuid, WFVariable=extension_input()),
        control("is.workflow.actions.conditional", has_input, 1),
        control("is.workflow.actions.choosefrommenu", menu, 0,
                WFMenuPrompt="Book from a photo",
                WFMenuItems=[{"WFItemType": 0, "WFValue": text_token([take])},
                             {"WFItemType": 0, "WFValue": text_token([choose])}]),
        control("is.workflow.actions.choosefrommenu", menu, 1,
                WFMenuItemTitle=take, WFMenuItemAttributedTitle=text_token([take])),
        action("is.workflow.actions.takephoto", UUID=new_uuid(), WFPhotoCount=1,
               WFCameraCaptureShowPreview=True),
        control("is.workflow.actions.choosefrommenu", menu, 1,
                WFMenuItemTitle=choose, WFMenuItemAttributedTitle=text_token([choose])),
        action("is.workflow.actions.selectphoto", UUID=new_uuid(), WFSelectMultiplePhotos=False),
        control("is.workflow.actions.choosefrommenu", menu, 2, UUID=menu_uuid),
        control("is.workflow.actions.conditional", has_input, 2, UUID=photo_if),
        # iPhone photos are HEIC and several megabytes; a JPEG of 1600 px
        # uploads in a moment and every server-side reader opens it.
        action("is.workflow.actions.image.resize", UUID=resized,
               WFImage=action_output(photo_if, "If Result"),
               WFImageResizeKey="Longest Edge", WFImageResizeLength=PHOTO_LONGEST_EDGE),
        action("is.workflow.actions.image.convert", UUID=jpeg,
               WFInput=action_output(resized, "Resized Image"), WFImageFormat="JPEG",
               WFImageCompressionQuality=0.8, WFImagePreserveMetadata=False),
        post(action_output(jpeg, "Converted Image"), photo_reply, key_uuid, deliver_to),
        control("is.workflow.actions.conditional", by_kind, 2, UUID=reply_if),
        action("is.workflow.actions.getvalueforkey", UUID=job_uuid,
               WFInput=action_output(reply_if, "If Result"),
               WFGetDictionaryValueType="Value", WFDictionaryKey="job_id"),
        control("is.workflow.actions.repeat.count", repeat, 0, WFRepeatCount=WAITS_IN_LOOP),
    ]
    # No If on the answer: "Contents of URL" is a file to Shortcuts, so a text
    # comparison on it imports broken (rig, 2026-10-30). A finished share
    # answers each ask at once, so the remaining asks cost nothing.
    actions += [wait(new_uuid(), job_uuid)]
    actions += [
        control("is.workflow.actions.repeat.count", repeat, 2, UUID=new_uuid()),
        wait(last_uuid, job_uuid, last=True),
        show(action_output(last_uuid, "Contents of URL")),
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
        # One question. A second one asking for a Kindle address could not
        # work: its value rode a header that always arrived empty, so setting
        # it only looked as though it had taken.
        "WFWorkflowImportQuestions": [
            {
                "ActionIndex": 0,
                "Category": "Parameter",
                "ParameterKey": "WFTextActionText",
                "Text": "Paste your book-search API key",
                "DefaultValue": "",
            },
        ],
        "WFWorkflowActions": actions,
    }


# The two shortcuts differ only in their name and in whose Kindle address the
# import question asks for. Both addresses stay OUT of the published files:
# /shortcut is unauthenticated, and a Kindle address is personal, so each is
# filled in at install time instead of being baked in.
# The second field is the recipient NAME the shortcut sends. The server maps it
# to an address through KINDLE_RECIPIENTS; an empty name means import only.
VARIANTS = {
    "": ("Download to Calibre", ""),
    "anca": ("Download to Calibre (Anca)", "anca"),
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
    name, deliver_to = VARIANTS[variant]
    with open(out, "wb") as fh:
        plistlib.dump(build(name, deliver_to), fh, fmt=plistlib.FMT_BINARY)
    print(f"wrote {out} ({name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
