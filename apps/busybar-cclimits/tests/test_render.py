import pytest

from busybar_cclimits import render
from busybar_cclimits.config import Config, DisplayConfig
from busybar_cclimits.limits import Feed, Window

NOW = 1_800_000_000.0


def feed(five=42.0, five_left=6130, week=18.0, week_left=3 * 86400, seen=NOW):
    return Feed(
        {
            "five_hour": Window(five, int(NOW + five_left)),
            "seven_day": Window(week, int(NOW + week_left)),
        },
        updated_at=seen,
        seen_at=seen,
    )


def by_id(elements):
    return {e.id: e for e in elements}


def test_default_layout():
    drawn = by_id(render.elements(feed(), NOW, DisplayConfig()))
    assert list(drawn) == [
        "cc0_label",
        "cc0_track",
        "cc0_fill",
        "cc0_clock",
        "cc1_label",
        "cc1_track",
        "cc1_fill",
        "cc1_text",
    ]
    assert drawn["cc0_label"].text == "5H" and drawn["cc1_label"].text == "WK"
    track = drawn["cc0_track"]
    assert (track.x, track.y, track.height) == (render.BAR_X, 2, 5)
    assert track.x + track.width + render.GAP + render.RIGHT_WIDTH["time"] == render.WIDTH
    assert drawn["cc1_track"].y == 10
    assert drawn["cc0_fill"].width == round(0.42 * track.width)
    assert drawn["cc0_fill"].fill_colors == [DisplayConfig().colors.ok]
    clock = drawn["cc0_clock"]
    assert clock.timestamp == str(int(NOW + 6130)) and clock.align == "bottom_right"
    assert (clock.x, clock.y) == (72, 11)
    assert drawn["cc1_text"].text == "18%" and drawn["cc1_text"].align == "top_right"


def test_colours_follow_the_thresholds():
    display = DisplayConfig()
    assert render.bar_color(74.9, display) == display.colors.ok
    assert render.bar_color(75, display) == display.colors.warn
    assert render.bar_color(90, display) == display.colors.critical


@pytest.mark.parametrize(
    ("used", "lit"), [(0, 0), (0.1, 1), (50, 16), (99.9, 31), (100, 32), (130, 32)]
)
def test_fill_width(used, lit):
    assert render.fill_width(used, 32) == lit


def test_empty_fill_keeps_its_id_and_is_invisible():
    drawn = by_id(render.elements(feed(five=0), NOW, DisplayConfig()))
    fill = drawn["cc0_fill"]
    assert fill.width == 1 and fill.fill_colors == [DisplayConfig().colors.track]


def test_far_reset_is_text_and_near_is_a_countdown():
    display = DisplayConfig.model_validate(
        {"rows": [{"window": "five_hour"}, {"window": "seven_day", "text": "time"}]}
    )
    far = by_id(render.elements(feed(week_left=3 * 86400 + 4 * 3600 + 30), NOW, display))
    assert far["cc1_text"].text == "3d4h"
    near = by_id(render.elements(feed(week_left=render.COUNTDOWN_UNDER), NOW, display))
    assert "cc1_clock" in near and "cc1_text" not in near


def test_a_passed_reset_draws_an_empty_bar_and_dashes():
    drawn = by_id(render.elements(feed(five=97, five_left=-1), NOW, DisplayConfig()))
    assert drawn["cc0_text"].text == "--" and "cc0_clock" not in drawn
    assert drawn["cc0_fill"].fill_colors == [DisplayConfig().colors.track]


def test_one_row_is_centred_and_a_missing_window_is_dashes():
    display = DisplayConfig.model_validate({"rows": [{"window": "spend_limit", "text": "percent"}]})
    drawn = by_id(render.elements(feed(), NOW, display))
    assert drawn["cc0_label"].text == "$" and drawn["cc0_track"].y == 6
    assert drawn["cc0_text"].text == "--"
    assert drawn["cc0_track"].width == 72 - render.BAR_X - 17 - render.GAP


def test_no_text_and_no_label_is_a_full_width_bar():
    display = DisplayConfig.model_validate(
        {"rows": [{"window": "five_hour", "text": "none", "label": ""}]}
    )
    (track, fill) = render.elements(feed(), NOW, display)
    assert (track.x, track.width) == (0, 72) and fill.width == round(0.42 * 72)


def test_visible():
    display = DisplayConfig()
    assert not render.visible(None, NOW, display)
    assert not render.visible(Feed({}, None, NOW), NOW, display)
    assert render.visible(feed(seen=NOW - 86400), NOW, display)
    shy = DisplayConfig(show="with_claude", idle_minutes=15, keep_above=80)
    assert render.visible(feed(seen=NOW - 15 * 60), NOW, shy)
    assert not render.visible(feed(seen=NOW - 15 * 60 - 1), NOW, shy)
    assert render.visible(feed(five=85, seen=NOW - 86400), NOW, shy)
    assert not render.visible(feed(five=85, five_left=-1, seen=NOW - 86400), NOW, shy)


def test_drawing_payload():
    config = Config()
    elements = render.elements(feed(), NOW, config.display)
    payload = render.drawing(elements, NOW + 180, "busybar-cclimits", 50).payload()
    assert payload["priority"] == 50 and payload["application_name"] == "busybar-cclimits"
    assert {e["display_until"] for e in payload["elements"]} == {str(int(NOW + 180))}
    assert all(e["id"] for e in payload["elements"])
    texts = [e for e in payload["elements"] if e["type"] == "text"]
    assert all(e["font"] == "small" for e in texts)
    # The bar's API takes colours with their alpha byte.
    assert all(len(e["color"]) == 9 for e in texts)
    assert render.key(elements) == render.key(render.elements(feed(), NOW, config.display))


THREE = DisplayConfig.model_validate(
    {
        "rows": [
            {"window": "five_hour"},
            {"window": "seven_day", "text": "percent"},
            {"window": "seven_day_fable"},
        ]
    }
)


def test_three_rows_in_the_tiny_font():
    f = feed()
    f.windows["seven_day_fable"] = Window(57, int(NOW + 6 * 3600))
    drawn = by_id(render.elements(f, NOW, THREE))
    assert [drawn[f"cc{n}_track"].y for n in range(3)] == [0, 6, 12]
    assert {drawn[f"cc{n}_track"].height for n in range(3)} == {4}
    assert drawn["cc2_label"].text == "FB" and drawn["cc2_label"].font == "tiny"
    assert drawn["cc0_label"].y == -1  # tiny glyphs start a row below the anchor
    # The countdown's 5-pixel digits: from the top row down, and the last
    # row's kept on the panel (rows 11-15).
    assert drawn["cc0_clock"].y == 9 and drawn["cc2_clock"].y == 20
    assert drawn["cc1_text"].font == "tiny"


def test_countdown_rows_for_one_and_two_rows_are_unchanged():
    assert render.ROWS[2].countdown_y(2) == 11 and render.ROWS[2].countdown_y(10) == 19
    assert render.ROWS[1].countdown_y(6) == 15
