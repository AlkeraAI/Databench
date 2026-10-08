# The image a local developer box runs from (the `localdev` compute provider).
#
# A privileged Ubuntu container that stands in for a remote machine on a
# developer's Mac (arm64) or Linux machine: the backend renders the same node
# bootstrap it renders for any machine and runs it here as the container's
# command. The bootstrap runs the repo's sandbox prerequisites (gVisor, the
# chat rootfs, the firewall), then the daemon from the developer's source tree,
# mounted read-only. This image only carries what that boot expects to find
# already installed, so a restart never waits on apt.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
# Everything the node bootstrap and the box's sandbox prerequisites expect to find,
# so a boot (or a recreated box) re-checks instead of reinstalling. The
# prerequisites install their packages only when one is missing, and do so
# without refreshing apt's index, which this image does not keep.
#   - the prerequisites' own list: uidmap nftables util-linux acl iproute2
#     bzip2 ca-certificates curl gnupg e2fsprogs quota;
#   - netbase, which gives nftables /etc/protocols (the ruleset names ipv6-icmp);
#   - socat, which forwards the developer machine's localhost ports into the box;
#   - tini as PID 1, so `docker stop` reaches the daemon's supervisor as a signal.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        uidmap nftables util-linux acl iproute2 bzip2 ca-certificates curl gnupg e2fsprogs quota \
        jq unzip netbase socat git tini \
    && rm -rf /var/lib/apt/lists/*

# gVisor (runsc) from gVisor's apt repository, the source the prerequisites
# install it from on a host.
RUN install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /etc/apt/keyrings/gvisor-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
        >/etc/apt/sources.list.d/gvisor.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends runsc \
    && rm -rf /var/lib/apt/lists/* \
    && runsc --version

# The same uv the repo's CI pins, so `uv sync --frozen` reads the committed lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.20 /uv /uvx /usr/local/bin/

# uv's caches live on volumes the provider mounts, shared by every local box,
# so a second box (or a recreated one) syncs the daemon's venv without
# downloading it again.
ENV UV_CACHE_DIR=/opt/uv-cache \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python

ENTRYPOINT ["/usr/bin/tini", "--"]
