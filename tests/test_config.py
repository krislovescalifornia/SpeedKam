# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Config loading: defaults, attribute access, and the untracked
`*.local.yaml` overlay that keeps real secrets out of git (see Step 1)."""
from speedkam.config import load_config, resolve_config


def test_defaults_when_no_path():
    cfg = load_config(None)
    assert cfg["camera"]["width"] == 1280
    assert cfg["backup"]["enabled"] is False
    # Section exposes keys as attributes too.
    assert cfg.camera.backend == "opencv"
    assert cfg.speed.display_units == "mph"


def test_user_file_overrides_defaults(tmp_path):
    base = tmp_path / "config.yaml"
    base.write_text("camera:\n  width: 640\nbackup:\n  enabled: true\n", encoding="utf-8")
    cfg = load_config(base)
    assert cfg["camera"]["width"] == 640          # overridden
    assert cfg["camera"]["height"] == 720         # default preserved (deep merge)
    assert cfg["backup"]["enabled"] is True


def test_local_overlay_wins_and_merges(tmp_path):
    base = tmp_path / "config.yaml"
    base.write_text(
        "backup:\n  enabled: true\n"
        '  url: "https://placeholder.example/x"\n'
        '  secret: "CHANGE-ME"\n',
        encoding="utf-8",
    )
    # Sibling overlay: config.yaml -> config.local.yaml
    (tmp_path / "config.local.yaml").write_text(
        'backup:\n  secret: "REAL-SECRET"\n  url: "https://real.example/recv"\n',
        encoding="utf-8",
    )
    cfg = load_config(base)
    assert cfg["backup"]["secret"] == "REAL-SECRET"        # overlay wins
    assert cfg["backup"]["url"] == "https://real.example/recv"
    assert cfg["backup"]["enabled"] is True                # base key survives merge
    assert cfg["backup"]["timeout"] == 30                  # default survives too


def test_no_overlay_is_fine(tmp_path):
    base = tmp_path / "config.yaml"
    base.write_text("web:\n  port: 9000\n", encoding="utf-8")
    cfg = load_config(base)
    assert cfg["web"]["port"] == 9000
    assert cfg["web"]["host"] == "0.0.0.0"


def test_overlay_naming_respects_stem(tmp_path):
    # config.test.yaml -> config.test.local.yaml (not config.local.yaml)
    base = tmp_path / "config.test.yaml"
    base.write_text("web:\n  port: 1111\n", encoding="utf-8")
    (tmp_path / "config.test.local.yaml").write_text("web:\n  port: 2222\n", encoding="utf-8")
    assert load_config(base)["web"]["port"] == 2222


def test_resolve_config_reports_source_of_each_leaf(tmp_path):
    # This is what powers /api/config/effective: every value must name the
    # layer that actually set it, so "which file changed this?" is answerable.
    base = tmp_path / "config.yaml"
    base.write_text("camera:\n  width: 640\nbackup:\n  enabled: true\n",
                    encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text(
        "camera:\n  fps: 60\nbackup:\n  secret: REAL\n", encoding="utf-8")
    section, source_of, layers = resolve_config(base)

    # The merged section is byte-for-byte what load_config returns.
    assert dict(section["camera"]) == dict(load_config(base)["camera"])
    # Each leaf names its winning layer.
    assert source_of["camera.width"] == "config.yaml"      # base set it
    assert source_of["camera.height"] == "defaults"        # never overridden
    assert source_of["camera.fps"] == "config.local.yaml"  # overlay set it
    assert source_of["backup.secret"] == "config.local.yaml"
    assert source_of["backup.enabled"] == "config.yaml"
    assert source_of["speed.display_units"] == "defaults"
    # Layers are reported in application order, existence included.
    assert layers == [("defaults", True), ("config.yaml", True),
                      ("config.local.yaml", True)]


def test_resolve_config_reports_missing_overlay(tmp_path):
    # A missing config.local.yaml is a common surprise -- it must be reported,
    # not silently absent.
    base = tmp_path / "config.yaml"
    base.write_text("web:\n  port: 9000\n", encoding="utf-8")
    _section, source_of, layers = resolve_config(base)
    assert layers == [("defaults", True), ("config.yaml", True),
                      ("config.local.yaml", False)]
    assert source_of["web.port"] == "config.yaml"
    assert source_of["web.host"] == "defaults"


def test_resolve_config_none_path_is_defaults_only():
    section, source_of, layers = resolve_config(None)
    assert layers == [("defaults", True)]
    assert section["camera"]["width"] == 1280
    assert source_of["camera.width"] == "defaults"
