import pytest

from busybar_cclimits import render
from busybar_cclimits.samples import DEMO_STEPS, SAMPLES, Sample

NOW = 1_800_000_000.0


@pytest.mark.parametrize("name", list(SAMPLES))
def test_every_sample_draws(name):
    sample = SAMPLES[name]
    display = sample.display_config()
    elements = render.elements(sample.feed(NOW), NOW, display)
    ids = [e.id for e in elements]
    assert len(ids) == len(set(ids)) and len(ids) <= 100  # the bar keeps at most 100
    for e in elements:
        width = getattr(e, "width", None) or 0
        right = e.x if e.align and e.align.endswith("right") else e.x + width
        assert 0 <= e.x <= render.WIDTH and right <= render.WIDTH
    assert render.visible(sample.feed(NOW), NOW, display)


def test_demo_steps_fill_up():
    widths = [
        render.elements(Sample("", five, week=week).feed(NOW), NOW, Sample("", 0).display_config())[
            2
        ].width
        for five, week in DEMO_STEPS
    ]
    assert widths == sorted(widths) and len(set(widths)) == len(widths)
