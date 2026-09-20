#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Role: Verifies Gemini CLI block behavior.
# File Name: F5.21_gemini_cli_block.py
# Author: Alexandre EL
# Email: alex@hackinvent.com
# Created Date: 2026-05-19
# -----------------------------------------------------------------------------

"""F5.21 - Gemini CLI block.

The test injects a fake `gemini` executable and verifies that the block calls
Gemini CLI with `-p`, publishes stdout, renders its block-owned UI, and works
in both centralized and zeromq_active runtimes.
"""

# Test cases:
# - FB1/FB2/FB7 - Run text -> Gemini CLI -> display in centralized runtime and verify `gemini -p` receives a prompt containing inputs and instruction.
# - FB1/FB2/FB7 - Run the same graph in zeromq_active runtime and verify stdout publication through the generic active worker.
# - FB3/FB6 - Configure model/extra args and verify the subprocess command includes them before `-p`.
# - FB4/FB6 - Execute a second output instruction and verify each output stores command metadata and stdout.
# - FB5 - Reject an oversized prompt before launching the Gemini CLI.
# - UI - Render modal/inspector/node-card and verify block-owned tabs, bindings, assets, and last-command display.

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blocs.gemini_cli.block import GeminiCliBlock
from bloxsmith_app.block_runtime import BlockRuntimeContext
from bloxsmith_app.block_ui import render_block_inspector_panel, render_block_modal, render_block_node_card
from ui_smoke_common import (
    create_run_api,
    data_edge,
    display_node,
    expect,
    graph_payload,
    isolated_server,
    text_node,
    wait_for_run_terminal,
)
from urllib.parse import quote
from block_test_packages import install_test_package, release_key, surface_payload


@contextmanager
def fake_gemini_cli(response_text: str = "fake gemini response"):
    """Expose a fake `gemini` binary that records argv and prints a response."""

    with TemporaryDirectory(prefix="bloxsmith-fake-gemini-") as tmp:
        temp_dir = Path(tmp)
        capture_path = temp_dir / "gemini_calls.jsonl"
        binary_path = temp_dir / "gemini"
        binary_path.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

capture_path = Path(os.environ["CW_FAKE_GEMINI_CAPTURE"])
capture_path.parent.mkdir(parents=True, exist_ok=True)
capture = {"argv": sys.argv[1:]}
if "-p" in sys.argv:
    index = sys.argv.index("-p")
    if index + 1 < len(sys.argv):
        capture["prompt"] = sys.argv[index + 1]
with capture_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(capture, ensure_ascii=False) + "\\n")
print(os.environ.get("CW_FAKE_GEMINI_RESPONSE", "fake gemini response"))
sys.exit(int(os.environ.get("CW_FAKE_GEMINI_EXIT", "0")))
""",
            encoding="utf-8",
        )
        binary_path.chmod(0o755)

        old_path = os.environ.get("PATH", "")
        old_capture = os.environ.get("CW_FAKE_GEMINI_CAPTURE")
        old_response = os.environ.get("CW_FAKE_GEMINI_RESPONSE")
        old_exit = os.environ.get("CW_FAKE_GEMINI_EXIT")
        os.environ["PATH"] = f"{temp_dir}{os.pathsep}{old_path}"
        os.environ["CW_FAKE_GEMINI_CAPTURE"] = str(capture_path)
        os.environ["CW_FAKE_GEMINI_RESPONSE"] = response_text
        os.environ["CW_FAKE_GEMINI_EXIT"] = "0"
        try:
            yield capture_path
        finally:
            os.environ["PATH"] = old_path
            if old_capture is None:
                os.environ.pop("CW_FAKE_GEMINI_CAPTURE", None)
            else:
                os.environ["CW_FAKE_GEMINI_CAPTURE"] = old_capture
            if old_response is None:
                os.environ.pop("CW_FAKE_GEMINI_RESPONSE", None)
            else:
                os.environ["CW_FAKE_GEMINI_RESPONSE"] = old_response
            if old_exit is None:
                os.environ.pop("CW_FAKE_GEMINI_EXIT", None)
            else:
                os.environ["CW_FAKE_GEMINI_EXIT"] = old_exit


def gemini_cli_node(*, two_outputs: bool = False, max_prompt_chars: int = 250000) -> dict:
    """Return a test Gemini CLI node document."""

    outputs = [
        {
            "id": 1,
            "name": "out",
            "title": "Out",
            "emits": ["message/*", "text/plain"],
            "multiplicity": "many",
            "instruction": "Return a concise answer from @in.",
        }
    ]
    if two_outputs:
        outputs.append(
            {
                "id": 2,
                "name": "summary",
                "title": "Summary",
                "emits": ["message/*", "text/plain"],
                "multiplicity": "many",
                "instruction": "Summarize @in.",
            }
        )
    return {
        "id": "gemini-cli-1",
        "kind": "gemini_cli",
        "title": "Gemini CLI test",
        "position": {"x": 360, "y": 120},
        "inputs": [
            {"id": 1, "name": "in", "title": "In", "accepts": ["message/*", "text/plain"], "multiplicity": "many"}
        ],
        "outputs": outputs,
        "config": {
            "gemini_binary": "gemini",
            "model": "gemini-test-model",
            "extra_args": "--output-format json",
            "timeout_sec": 10,
            "max_prompt_chars": max_prompt_chars,
        },
    }


def run_gemini_cli_case(runtime_mode: str) -> None:
    """TC1/TC2 - Verify Gemini CLI execution and output propagation in one runtime mode."""

    with fake_gemini_cli(response_text=f"fake gemini {runtime_mode}") as capture_path:
        with isolated_server() as server:
            # Les surfaces sont des assets de release : le bundled kind n'en sert aucun.
            model = install_test_package(server, "gemini_cli")
            key = quote(release_key(model), safe="")
            served = lambda payload, suffix: next(
                asset["path"] for asset in payload["assets"] if asset["path"].endswith(suffix))
            document = graph_payload(
                f"F5 Gemini CLI {runtime_mode}",
                [
                    text_node("text-1", "Texte Gemini", "hello gemini", 80, 120),
                    gemini_cli_node(),
                    display_node("display-1", "Affichage", 680, 120),
                ],
                [
                    data_edge("edge-text-gemini", "text-1", 1, "gemini-cli-1", 1),
                    data_edge("edge-gemini-display", "gemini-cli-1", 1, "display-1", 1),
                ],
            )
            created = create_run_api(server, document, runtime_mode=runtime_mode)
            run = wait_for_run_terminal(server, str(created.get("run_id") or ""), timeout_sec=20)

        calls = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        expect(run.get("status") == "success", f"Le run Gemini CLI {runtime_mode} doit reussir.")
        expect(run.get("output_values", {}).get("gemini-cli-1:1", {}).get("value").strip() == f"fake gemini {runtime_mode}", "stdout Gemini CLI incorrect.")
        argv = calls[-1].get("argv", []) if calls else []
        expect("-p" in argv, "Gemini CLI doit etre appele avec -p.")
        expect(argv[:2] == ["-m", "gemini-test-model"], "Le modele configure doit etre transmis avec -m.")
        expect("--output-format" in argv and "json" in argv, "Les arguments additionnels doivent etre transmis.")
        prompt = str(calls[-1].get("prompt") or "")
        expect("hello gemini" in prompt, "Le prompt Gemini CLI doit contenir l'input texte.")
        expect("Return a concise answer" in prompt, "Le prompt Gemini CLI doit contenir l'instruction.")
        logs = "\n".join(run.get("node_logs", {}).get("gemini-cli-1", []))
        expect("[gemini-cli-cmd]" in logs and " -p " in logs, "Les logs doivent exposer la commande Gemini CLI avec -p.")


def test_multiple_outputs_and_prompt_guard() -> None:
    """TC3 - Execute multiple output instructions and reject an oversized prompt."""

    with fake_gemini_cli(response_text="multi") as capture_path:
        with isolated_server() as server:
            document = graph_payload(
                "F5 Gemini CLI multi",
                [
                    text_node("text-1", "Texte Gemini", "multi hello", 80, 120),
                    gemini_cli_node(two_outputs=True),
                ],
                [data_edge("edge-text-gemini", "text-1", 1, "gemini-cli-1", 1)],
            )
            created = create_run_api(server, document, runtime_mode="centralized")
            run = wait_for_run_terminal(server, str(created.get("run_id") or ""), timeout_sec=20)

        calls = [json.loads(line) for line in capture_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        expect(run.get("status") == "success", "Le run multi-output Gemini CLI doit reussir.")
        expect(len(calls) == 2, "Gemini CLI doit etre appele une fois par sortie.")
        expect(run.get("output_values", {}).get("gemini-cli-1:2", {}).get("value").strip() == "multi", "La sortie 2 doit publier stdout.")
        result = run.get("results", {}).get("gemini-cli-1", {})
        expect("last_gemini_command" in str(result), "La metadata doit conserver la derniere commande Gemini CLI.")

    block = GeminiCliBlock()
    result = block.execute_runtime(
        BlockRuntimeContext(
            run_id="unit-run",
            node_id="gemini-cli-guard",
            kind="gemini_cli",
            title="Gemini guard",
            config={"gemini_binary": "gemini", "timeout_sec": 10, "max_prompt_chars": 8},
            inputs={"in": "0123456789"},
            input_content_types={"in": "text/plain"},
            input_message="0123456789",
            input_ports=(),
            output_ports=(SimpleNamespace(id=1, name="out", instruction="too long"),),
            root_dir=ROOT,
            run_dir=ROOT,
        )
    )
    expect(result.status == "failed", "Un prompt trop long doit etre refuse avant execution.")
    expect("trop long" in result.error, "Le message d'erreur doit expliquer la limite de prompt.")


def test_gemini_cli_ui_contract() -> None:
    """TC4 - Render Gemini CLI block-owned modal, inspector, node-card, and assets."""

    node = gemini_cli_node()
    rendered = render_block_modal("gemini_cli", {"node": node, "runtime": {}})
    html = str(rendered.get("html") or "")
    assets = rendered.get("assets") or []
    css = (ROOT / "blocs/gemini_cli/assets/css/block_modal.css").read_text(encoding="utf-8")
    js = (ROOT / "blocs/gemini_cli/assets/js/block_modal.js").read_text(encoding="utf-8")

    expect("cw-gemini-cli-modal" in html, "Le modal Gemini CLI doit venir du bloc.")
    expect('data-block-runtime-refresh="autonomous"' in html, "Le modal Gemini CLI doit gerer son refresh runtime.")
    expect('data-gemini-tab-id="output-1"' in html, "Le modal doit exposer l'onglet instruction de sortie.")
    expect('data-gemini-tab-id="attributes"' in html, "Le modal doit exposer l'onglet Attributs.")
    expect('data-gemini-tab-id="last-cmd"' in html, "Le modal doit exposer l'onglet Last cmd.")
    expect('data-block-output-field="instruction"' in html, "L'instruction doit rester liee a output.instruction.")
    expect('data-block-config-field="gemini_binary"' in html, "Le binaire Gemini doit etre editable.")
    expect('data-block-config-field="model"' in html, "Le modele Gemini doit etre editable.")
    expect('data-block-config-field="extra_args"' in html, "Les arguments additionnels doivent etre editables.")
    expect(".gemini-modal-panel[hidden]" in css, "Le CSS doit cacher les panels inactifs.")
    expect("export function mount" in js, "Le JS doit monter le modal via le registre block UI.")

    inspector = render_block_inspector_panel("gemini_cli", {"node": node})
    inspector_html = str(inspector.get("html") or "")
    expect("cw-gemini-cli-inspector" in inspector_html, "L'inspector Gemini CLI doit venir du bloc.")
    expect('data-block-output-field="instruction"' in inspector_html, "L'inspector doit editer l'instruction.")

    card = render_block_node_card("gemini_cli", {"node": node})
    card_html = str(card.get("html") or "")
    expect("data-gemini-cli-node-card" in card_html, "La node-card Gemini CLI doit venir du bloc.")


def main() -> None:
    test_gemini_cli_ui_contract()
    test_multiple_outputs_and_prompt_guard()
    run_gemini_cli_case("centralized")
    run_gemini_cli_case("zeromq_active")
    print("[ok] F5.21_gemini_cli_block")


if __name__ == "__main__":
    main()
