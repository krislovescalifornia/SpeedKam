# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The residual battery (tools/measure_residual.py) is a living regression: with
the completed geometry gate of record ('proposed'), every documented adversary
must be rejected and no real car may be lost. This is the machine-checked form of
Step 2 -- "measure what the physics gate leaks" -- so a future change that opens a
hole fails here."""
import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "measure_residual",
    os.path.join(os.path.dirname(__file__), "..", "tools", "measure_residual.py"))
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)


def _run(enforce):
    residual, false_neg = [], []
    for c in mr.build_battery():
        status, _, _ = mr.evaluate(c, mr.THRESHOLDS[enforce])
        counted = status == "ok"
        if c.kind == "junk" and counted:
            residual.append(c.name)
        if c.kind == "car" and not counted:
            false_neg.append(c.name)
    return residual, false_neg


def test_proposed_gate_leaks_nothing_and_keeps_cars():
    residual, false_neg = _run("proposed")
    assert residual == [], f"junk leaked through the geometry gate: {residual}"
    assert false_neg == [], f"real cars wrongly rejected: {false_neg}"


def test_shipped_gate_leaks_the_teleport():
    # Documents WHY the new gates were added: with them off, the noise-teleport
    # phantom (a straight, smooth-area track that jumps between noise blobs)
    # slips past every shipped gate. The acceleration bound is what closes it.
    residual, false_neg = _run("shipped")
    assert "noise_teleport" in residual
    assert false_neg == []


def test_every_case_has_a_distinct_expected_outcome():
    cases = mr.build_battery()
    assert {c.kind for c in cases} == {"car", "junk"}
    assert len([c for c in cases if c.kind == "car"]) >= 2
