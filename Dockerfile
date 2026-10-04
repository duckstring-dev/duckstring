# The Duckstring container image — a Duck (or Catchment) runtime for serverless/container launchers.
#
# A Fargate Duck runs `python -m duckstring.duck <args>`: the FargateLauncher supplies the args as the
# task's container command override (--pond/--major/--version/--catchment/--token/--root/--source-path/
# --data-root), so the Duck fetches its source artifact over the duck channel and boots — the same
# remote-boot path the EC2 launcher uses, just in a container instead of on an instance.
#
# Build (needs a wheel in ./dist — `python -m build` first, or CI downloads the release artifact):
#   python -m build && docker build -t duckstring .
# The ENTRYPOINT is `python -m`, so `docker run duckstring duckstring.duck --help` works, and Fargate's
# command override is just `["duckstring.duck", "--pond", ...]`.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DUCKSTRING_STATE_ROOT=/var/lib/duckstring \
    UV_CACHE_DIR=/var/lib/duckstring/uv-cache \
    UV_LINK_MODE=hardlink

# A non-root runtime user with a writable hot-state root (data lives in the object store, not here).
RUN useradd --create-home --uid 10001 duck

# The prebuilt wheel (bundles the schema + web UI). Installed rather than `pip install duckstring` so the
# image always matches the release being built, not whatever PyPI has. The [aws] extra (s3fs + boto3) is
# included: a Duck launched on EC2/Fargate always reads/writes the data plane on S3. The wheel is KEPT:
# a Duck whose Pond declares its own environment installs this same Duckstring into it, from this file
# (duckstring.environments.duckstring_requirement), so it works for an unreleased build too.
COPY dist/*.whl /opt/duckstring/

# Installed with uv, hardlinked from a uv cache kept in the image (plans/pond-environments.md). The
# installed files ARE the cache's files, so the cache adds almost nothing to the image, and a Pond
# environment built when a Duck starts links Duckstring's dependencies from it instead of downloading them
# again (measured: 12.7 s -> 3.5 s). One layer, so the chown (uv needs a writable cache) copies nothing.
RUN pip install uv \
    && uv pip install --system "$(ls /opt/duckstring/*.whl)[aws]" \
    && mkdir -p /var/lib/duckstring \
    && chown -R duck:duck /var/lib/duckstring
USER duck
WORKDIR /var/lib/duckstring

ENTRYPOINT ["python", "-m"]
CMD ["duckstring.duck", "--help"]
