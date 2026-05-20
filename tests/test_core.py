from __future__ import annotations

import duckstring
from duckstring import ripple
from duckstring.core import Catchment, Pond, Ripple, Trickle


def test_ripple_exported():
    assert hasattr(duckstring, "ripple")


def test_ripple_plain_decorator():
    @ripple
    def load(pond):
        pass

    assert load is not None
    assert callable(load)


def test_ripple_plain_decorator_returns_function():
    def original(pond):
        return 42

    wrapped = ripple(original)
    assert wrapped is original


def test_ripple_with_args():
    @ripple
    def load(pond):
        pass

    @ripple(parents=[load])
    def clean(pond):
        pass

    assert callable(clean)


def test_ripple_with_name_arg():
    @ripple(name="custom", parents=[])
    def _fn(pond):
        pass

    assert callable(_fn)


def test_stub_classes_importable():
    assert Catchment is not None
    assert Pond is not None
    assert Ripple is not None
    assert Trickle is not None
