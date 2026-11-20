"""What a person's own Claude or ChatGPT is served through the door stays
byte for byte (Standalone Tim shot 2, card C1, 2026-11-17).

Real people use the connector door today, and the nightly persona run scores
it. Tim's own loop may read a shorter form of a tool (``ToolSpec.short``),
but each tool's name, title, description, input schema, and annotations, as
the door's ``tools/list`` serves them, are pinned here by hash, taken from
main before the catalog was split. A change to what the door serves is a
deliberate act: change the words, then the pin, in the same PR, and say why.
"""

from __future__ import annotations

import hashlib
import json

from core.tools import catalog
from core.tools.mcp_stdio import McpServer

PINNED = {
    "add_receipt": "aece7e9c7a24633104a7dfe115a030dcfdec2b6918c5177d0163b768056ab75a",
    "agree_to_readings": "d29620cac33dbb2defa14339ab89ea44b0aabddf17fb4296b010bd15bc63ff52",
    "ask_for_ok": "5bb3d87ecef6f500191938afbf0d5e5ecdc259a08a57877d97c8b2cae3a70208",
    "ask_the_rules": "d4be0fe1af0580ff79fea190d6c448d485003ff04013a44df5474c73a9028130",
    "bank_statement": "11988fa6bb83c93a6c767e2229ec8fab0ec3459779b6d92855a5f0480a7439c2",
    "board_pack": "8aff49410ace8f4da0cf9c313a4d198648a30c8f9f5dcbfdfc0e18cfe0f83ac9",
    "books": "b702bd92bae3c54c1f5ae849a99f612c30b1a0e867498921579b859fa96fc8b0",
    "catch_up_link": "7dc36020b1cfdf329948aa4cbe435cc2a00d09d3d5c1a816c3d1ff892d3d0dec",
    "close_status": "45f46ec72c0c1741f9ee592d1c97368d60413ab12c33ba4f18a084072db6b0bf",
    "close_year": "179c45e5d6be0a74c23cef829ea5cf2fcfb5e58dad68f2b7ac44b84968a550f3",
    "count_cash": "c21ae420ad405d8ca702f271aa41c30d5da14837e7f88a574315585043d3e43a",
    "cpa_export": "32ed9c18966005470a5ffe64a7ea0ba3433cd5f36118cf31218c62f11aeb72c4",
    "deadlines": "86d456d08d5b8c15e90070c642c0407a54f6977ee47e353df73b091340356706",
    # decide_card took its third decision, take_back (the owner's call of
    # 2026-11-18, A06): a person's own AI may take back their yes or no too.
    "decide_card": "d036d959580f37967d9813629e97f868adf454ba379753313c16ea74f5051b09",
    "donor_letters": "ffdd6e27c5bf14d2dfe65fc09b1975d7332f74ae99fdfaf75b0b19bdef7435f7",
    "draft_rule": "a14aca17987c41e6f65f3f1080d5d55c632aa552d693f3c972f04b55908901ba",
    "expense_reports": "e9c9ead1959c18607a7a3cccccfb2027d84c901ac642f9e1d91c17801df97e22",
    "explain_bank_line": "f6b0b88e5e6e93c64303f2ab40964fff81a1b6be53abaee6959af19d283eafd8",
    "explain_cash": "e6d9417f679bd35f28e82ab3e343ee94dce9d16cde39fa710a562f36ba92d403",
    "find_invoices": "1ed6a0bc973829efd5b13c718f3b6285f828f7f532a8cb4ec6df819c55aec7f4",
    "handbook_changes": "e3851957b6c79403f2e5aa7546119af32054ef85a2e8df27cc744df0b339601b",
    "keep_own_lines": "d75bffc32a60ba1c2b5d8bdeb8951cd5491322e76f4a4651bfcaf244b3a19596",
    "keep_payers": "74e4e0e1cb490a1c2fd227fb1c2c5e0327ba90b1afc8f07b3268a24318cce71d",
    "keep_school_list": "550c9e9a626a1f9c42079eb44421acb52316ea59cbc297715ba814d974026279",
    "keep_tax_facts": "ec47b572e82d11f6a948700ad58968fc5c63a5a2e154e42cc657b0eeb6bfe393",
    "keep_travel": "51edeaffb4a8ce15e7f6f5b20ff245e889e9f135eba75099c3b9eb79d68242ff",
    "letter_details": "6712821f42b843ac4666c546e38174e6010c5efb5632534e163ff8bdce9480a4",
    "month_report": "a723d1ff6a6a2130e343818f86845f6cfab09e1460826da81278415b0585e765",
    "open_payables": "e5ad900384b28d7995a7826973800a8c2af2102510e73817265dff51ddfe10e1",
    # paid_it_myself: `again` added 2026-11-20; example amount changed 2026-11-20 (export)
    "paid_it_myself": "bd413a2fbf1db050499a2aa611742b37f4d1cccce3b03785de6126977ff8eaf7",
    "pick_card_receipt": "80b19677ef0e3d0e3249feb814d6aa5e8e827f7800a5ddc8df04dd014c9d8d76",
    "receipts_to_read": "bf06e346268ada40480bb126196153b293dd941c272684ebbd9e0c0756068310",
    "recent_runs": "5a5938394847a274a980884bd41aa7a75dfb19644171ef151437a7a1f9cb7c41",
    "record_reading": "480d64e2c4fee7d6d220bbb83f555f4d6283eba4513f91869a4ba01b41989752",
    "reopen_year": "506d22731815909db017b0ca77a120a8a774974229c2177ccd267e557f8a92a8",
    "send_month_for_review": "7d6280812288e24956cfb7baf70ce22ad4d6da62756e5e65b288f0d7c69a5282",
    "set_amount": "e2b7f65813b9cb86d0db34f3ef68e0343e29b64a37cc4c9116fbe3f9cb512d5c",
    "set_budget": "0898c6d289b0467896d7e282c4252e71103c03872f79b9ed9881b2a96103453d",
    "set_category": "e285a46877384f3ec3a023746c4c57d34a9a518e75931a10a1a8da24c3bdf1ee",
    "set_drive_purpose": "663374f3de7b08385be1df1b85b386f98f4615f0874c66d9468a9f0182d54995",
    "set_pay": "f83df11329d45d900ee7b83f20ad3feaa67ee613aebd225148d2dc981497675a",
    "set_program_share": "19634b71f59b982bb0829d22331ca8bc77a4a831dc57c91cf316643634a42b0f",
    "set_receipt_day": "860a3ef73a76b7f3e3576ab306593b00dd3d679beacb2577c897e8791d2e44ff",
    "split_line": "97c3359e7c5eb591b11b7ab9339261b58fba0e84cd0410ba9282f4a2ceca0868",
    "start_here": "c8949b04c2441e9a115fb7d9b5ad1dfdea20d0ee1d905f3d04cff81883b37fef",
    "tax_packet": "6ba06a22bfffd12d238b4b9b6908fec718e19cdaffd495d475643fab987cddd7",
    "waiting_cards": "f6e7ceab337a563ba84843cd5668c716daf780de0e8818554a1336ec006591ef",
    "why": "32afb78b4414babd2791b1b55b47b2407cbf04f548b174b2a322e2866a4a8443",
    "year_report": "67b4cc5233344eb82a051b67721a9f022d28d31100955795c4a2b0ccd56bd933",
}


class _Every:
    """Every tool the catalog can offer anyone, on any door, as one tool set."""

    def specs(self) -> list[catalog.ToolSpec]:
        found = {s.name: s for s in catalog.SPECS}
        found.update({v.name: v for v in vars(catalog).values() if isinstance(v, catalog.ToolSpec)})
        found.update({s.name: s for s, _ in catalog.ADDED})
        return list(found.values())

    def call(self, name: str, args: dict | None = None) -> dict:
        raise KeyError(name)


def served() -> dict[str, str]:
    """Each tool's hash, over exactly what the door's ``tools/list`` serves."""
    listed = McpServer(_Every()).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed is not None
    return {
        e["name"]: hashlib.sha256(
            json.dumps(e, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        for e in listed["result"]["tools"]
    }


def test_every_tool_the_door_serves_is_pinned():
    assert sorted(served()) == sorted(PINNED)


def test_what_the_door_serves_is_byte_for_byte_as_pinned():
    changed = [name for name, digest in served().items() if PINNED.get(name) != digest]
    assert changed == [], f"the door now serves different words for: {changed}"
