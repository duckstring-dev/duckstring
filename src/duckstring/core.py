from __future__ import annotations


def ripple(func=None, *, parents=None, name=None):
    """Decorator that registers a function as a Ripple in a Pond.

    Usage:
        @ripple
        def load(pond): ...

        @ripple(parents=[load])
        def clean(pond): ...
    """
    if func is not None:
        # Called as @ripple without arguments
        return func

    # Called as @ripple(...) with arguments — return a no-op decorator
    def decorator(f):
        return f

    return decorator


class Catchment:
    # TODO
    pass


class Pond:
    # TODO
    pass


class Ripple:
    # TODO
    pass


class Trickle:
    # TODO
    pass
