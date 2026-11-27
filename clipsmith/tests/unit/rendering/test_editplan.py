"""Edit plans: the validator keeps plans renderable, within budgets and varied."""

from __future__ import annotations

import itertools

import pytest

from clipsmith.rendering import editplan
from clipsmith.rendering.director import _piecewise, game_crop, main_pieces, positions

pytestmark = pytest.mark.unit


def _plan(**kw):
    base = {
        "payoff_t": 20.0, "cold_open": True, "teaser": {"from": 18.0, "to": 21.5},
        "segments": [{"from": 0, "layout": "stack"}, {"from": 18.5, "layout": "cam_big"}],
        "punch_ins": [{"t": 20.0, "dur": 1.2, "target": "face", "zoom": 1.2}],
        "sfx": [{"t": 20.0, "name": "boom"}],
        "caption_preset": "boxed", "emphasis": ["perro"], "hook_type": "question",
        "hook_look": "black_box", "hook_style": "voice_text",
    }
    base.update(kw)
    return base


def test_valid_plan_passes_untouched():
    plan, errors = editplan.validate(_plan(), 40.0, has_cam=True)
    assert errors == []
    assert plan["segments"][1]["layout"] == "cam_big"
    assert plan["hook_style"] == "text_only"  # never a voice over the cold-open payoff
    assert plan["emphasis"] == ["PERRO"]


def test_cam_layouts_replaced_without_facecam():
    plan, errors = editplan.validate(_plan(punch_ins=[]), 40.0, has_cam=False)
    assert all(not editplan.LAYOUTS[s["layout"]]["cam"] for s in plan["segments"])
    assert any("facecam" in e for e in errors)


def test_budgets_enforced():
    segs = [{"from": t, "layout": lay} for t, lay in
            [(0, "stack"), (3, "cam_big"), (6, "game_full"), (9, "stack"), (12, "cam_big"), (15, "game_top")]]
    pins = [{"t": t, "dur": 0.8, "target": "face", "zoom": 1.6} for t in (0.2, 2, 5, 8, 11, 14, 17)]
    plan, errors = editplan.validate(_plan(segments=segs, punch_ins=pins), 20.0, has_cam=True)
    assert len(plan["segments"]) - 1 <= 2  # 3 per 30 s on a 20 s clip
    assert len(plan["punch_ins"]) <= 2
    assert all(1.1 <= p["zoom"] <= 1.35 and p["t"] >= 0.5 for p in plan["punch_ins"])
    assert errors


def test_short_payoff_disables_cold_open_and_teaser_is_clamped():
    plan, _errors = editplan.validate(_plan(payoff_t=3.0, teaser={"from": 2, "to": 4}), 30.0, True)
    assert plan["cold_open"] is False
    plan, _ = editplan.validate(_plan(teaser={"from": 10, "to": 19}), 40.0, True)
    t = plan["teaser"]
    assert editplan.TEASER_MIN - 1e-6 <= t["to"] - t["from"] <= editplan.TEASER_MAX + 1e-6
    assert t["from"] <= 20.0 <= t["to"]


def test_variety_against_history():
    hist = [{"caption_preset": "boxed", "hook_type": "question", "hook_look": "black_box"}]
    plan, errors = editplan.validate(_plan(), 40.0, True, hist)
    assert plan["caption_preset"] != "boxed"
    assert plan["hook_look"] != "black_box"
    assert any("hook_type" in e for e in errors)  # sent back to the model to rewrite


def test_fallback_plan_is_valid():
    peaks = [(5.0, 6.0), (22.0, 14.0), (30.0, 9.0)]
    raw = editplan.fallback_plan(40.0, True, peaks)
    plan, errors = editplan.validate(raw, 40.0, True)
    assert plan["payoff_t"] == 22.0 and plan["cold_open"]
    assert not errors


def test_pieces_cover_the_clip_and_split_on_punch_ins():
    plan, _ = editplan.validate(_plan(), 40.0, True)
    pieces = main_pieces(plan, 40.0)
    assert pieces[0].a == 0 and pieces[-1].b == 40.0
    assert all(p.b > p.a for p in pieces)
    assert all(a.b == b.a for a, b in itertools.pairwise(pieces))
    zoomed = [p for p in pieces if p.zoom > 1]
    assert len(zoomed) == 1 and zoomed[0].layout == "cam_big"


def test_piecewise_expression():
    assert _piecewise([(0, 640), (10, 1090)]) == "if(lt(t,10.000),640,1090)"
    assert _piecewise([(0, 5)]) == "5"


def test_game_crop_avoids_facecam_on_wide_panels():
    cam = [30, 600, 450, 292]
    w, h, x, y = game_crop(1920, 1080, cam, 1080 / 770)
    assert x >= 480 and x + w <= 1920 and y + h <= 1080
    assert abs(w / h - 1080 / 770) < 0.02


def test_caption_positions_inside_safe_zone():
    for lay in editplan.LAYOUTS:
        cap_y, hook_y = positions(lay, True)
        assert 290 <= cap_y and cap_y + 360 <= 1440 + 120
        assert 290 <= hook_y <= 1440
