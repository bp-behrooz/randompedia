"""The shuffle must be reproducible from (seed, lang, count)."""
import random


def _shuffle(seed: str, lang: str, count: int, items: list[str]) -> list[str]:
    # Mirrors the seeding logic in randompedia.cli.main.
    rng = random.Random(f"{seed}|{lang}|{count}")
    out = list(items)
    rng.shuffle(out)
    return out


def test_same_inputs_produce_same_order():
    items = [f"a{i}" for i in range(100)]
    a = _shuffle("s", "en", 100, items)
    b = _shuffle("s", "en", 100, items)
    assert a == b


def test_different_seed_produces_different_order():
    items = [f"a{i}" for i in range(100)]
    a = _shuffle("s1", "en", 100, items)
    b = _shuffle("s2", "en", 100, items)
    assert a != b


def test_different_count_produces_different_order():
    items = [f"a{i}" for i in range(100)]
    a = _shuffle("s", "en", 100, items)
    b = _shuffle("s", "en", 200, items)
    assert a != b


def test_different_lang_produces_different_order():
    items = [f"a{i}" for i in range(100)]
    a = _shuffle("s", "en", 100, items)
    b = _shuffle("s", "de", 100, items)
    assert a != b
