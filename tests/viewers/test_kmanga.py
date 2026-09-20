from __future__ import annotations

import pytest
from PIL import Image

from getjmanga.viewers.kmanga import GRID, UNIT, descramble, service_hash, tile_order, xorshift32

SEED = 284797878


# --- the hash -----------------------------------------------------------------------


def test_service_hash_matches_what_the_viewer_sends():
    # Worked out by hand from the bundle: sorted `sha256(key)_sha512(value)` pairs joined
    # by commas, SHA-256'd, then SHA-512'd with the empty birthday cookie's pair appended.
    params = {"version": "6.0.0", "platform": "3", "episode_id": "1173"}
    digest = service_hash(params)
    assert len(digest) == 128
    assert digest == service_hash({"episode_id": 1173, "platform": 3, "version": "6.0.0"})
    assert digest != service_hash({**params, "episode_id": "1174"})
    assert digest != service_hash(params, birthday="2000-01", expires="1789672044")
    assert digest.startswith("30c4c626d4fa4a07")


# --- descrambling -------------------------------------------------------------------


def test_xorshift32_is_the_viewers_generator():
    # The first values of xorshift32 seeded with 1, as `Uint32Array` arithmetic gives them.
    values = xorshift32(1)
    assert [next(values) for _ in range(3)] == [270369, 67634689, 2647435461]


def tile_image(order, tile=(UNIT * 3, UNIT * 2), extra=(5, 3)):
    """A `GRID x GRID` image of flat-coloured tiles laid out in `order`, plus an edge strip."""
    width, height = tile[0] * GRID + extra[0], tile[1] * GRID + extra[1]
    image = Image.new("L", (width, height), 255)
    for destination, source in enumerate(order):
        x, y = destination % GRID * tile[0], destination // GRID * tile[1]
        image.paste(source * 10 + 10, (x, y, x + tile[0], y + tile[1]))
    return image


def scrambled_layout(seed=SEED):
    """Where the served page keeps each tile: the inverse of `tile_order()`.

    The viewer draws served tile `order[d]` at destination `d`, so the served page
    holds the page's tile `d` at position `order[d]`.
    """
    layout = [0] * (GRID * GRID)
    for destination, source in enumerate(tile_order(seed)):
        layout[source] = destination
    return layout


def test_descramble_puts_the_tiles_back():
    original = tile_image(range(GRID * GRID))
    scrambled = tile_image(scrambled_layout())
    assert scrambled.tobytes() != original.tobytes()
    assert descramble(scrambled, SEED).tobytes() == original.tobytes()


@pytest.mark.parametrize("seed", [0, 2**32])
def test_descramble_ignores_a_seed_out_of_range(seed):
    image = tile_image(range(GRID * GRID))
    assert descramble(image, seed) is image


def test_descramble_ignores_an_image_too_small_to_tile():
    image = Image.new("L", (GRID * UNIT - 1, 100), 0)
    assert descramble(image, SEED) is image
