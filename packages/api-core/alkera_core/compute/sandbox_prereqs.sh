#!/usr/bin/env bash
# What a workspace box needs before it can sandbox every chat.
#
# There are two sandbox modes, set by
# ALKERA_SANDBOX_MODE:
#   gvisor  the agent runs under gVisor (runsc) — a real boundary against
#           untrusted code, required for the multi-tenant pool. This installs
#           runsc and stages the rootfs every chat's container roots at: a
#           minimal Ubuntu with the agent's runtime dependencies, Python, uv
#           and micromamba, verified by digest and cached per box.
#   none    no boundary — allowed only on a single-tenant node (an enterprise
#           dedicated box, or a dev/RunPod box). No runsc, no rootfs.
#
# Either way this installs the uid tools, acl and iproute2, a modern Python
# (uv-managed, under /opt/alkera/python), uv and micromamba on the host — the
# default environment every chat gets is made from them — creates the parent
# cgroup slice the per-chat slices hang under (the per-chat uid and cgroup are
# kept in both modes — they are resource/ownership controls, not the
# boundary), turns on forwarding for the per-chat network namespaces, and
# loads the firewall: every chat uid is kept away from the instance metadata
# service; a chat's namespace reaches out through NAT, never the metadata
# service, never another chat, and nothing on the host but the (interface,
# port) pairs the daemon grants it in the chat_ports set. On a host the plane
# owns, new inbound is dropped except ssh (and any port named in
# SANDBOX_INBOUND_TCP_PORTS) and nothing else is forwarded; on a host its
# operator shares (ALKERA_SANDBOX_HOST_SCOPE=shared) every drop is scoped to
# the node's own links and uids, so the host's other services and containers
# are left as they were.
# gVisor's own network stack has no route of its own, so under runsc the
# firewall is defence in depth; a "none" box relies on it as the only metadata
# boundary.
#
# Idempotent: every step checks or replaces its own state, so a re-run on a
# provisioned box changes nothing. Run as root. The compute bootstrap calls it;
# it is also safe to run by hand.
set -euo pipefail

CHAT_UID_MIN=20000
CHAT_UID_MAX=59999
METADATA_IPV4=169.254.169.254
METADATA_IPV6=fd00:ec2::254
SANDBOX_MODE="${ALKERA_SANDBOX_MODE:-gvisor}"
# Whether the host firewall and forwarding MUST load: "required" (the default)
# refuses the boot when they cannot; "optional" logs and goes on. The compute
# bootstrap sets optional only for a none node inside a provider container
# that has no CAP_NET_ADMIN and no instance metadata service to fence (RunPod).
HOST_FIREWALL="${ALKERA_SANDBOX_HOST_FIREWALL:-required}"
# Whose host this is, from the provider's boot profile: "owned" (provisioned
# for the node alone) or "shared" (its operator runs other things on it).
HOST_SCOPE="${ALKERA_SANDBOX_HOST_SCOPE:-owned}"
SANDBOX_HOME="${ALKERA_SANDBOX_HOME:-/home/alkera}"
SANDBOX_NET="${ALKERA_SANDBOX_NET:-10.200.0.0/14}"
# The org workers: the host ids their user namespaces map (one 65536-id range
# per slot from ORG_UID_BASE) and the block their links to the host come from.
ORG_UID_BASE=10000000
ORG_UID_TOTAL=268435456
ORG_NET=10.204.0.0/16
RUNSC_RELEASE="${ALKERA_RUNSC_RELEASE:-latest}"
UV_RELEASE="${ALKERA_UV_RELEASE:-latest}"
MICROMAMBA_RELEASE="${ALKERA_MICROMAMBA_RELEASE:-latest}"
PYTHON_VERSION="${ALKERA_SANDBOX_PYTHON_VERSION:-3.12}"
INBOUND_TCP_PORTS="${SANDBOX_INBOUND_TCP_PORTS:-22}"
ENV_DIR=/etc/alkera
ENV_FILE="$ENV_DIR/sandbox.env"
NFT_FILE="$ENV_DIR/sandbox.nft"
SYSCTL_FILE=/etc/sysctl.d/90-alkera-sandbox.conf
TOOLS_ROOT=/opt/alkera
PYTHON_ROOT="$TOOLS_ROOT/python"
PYTHON_HOME="$PYTHON_ROOT/current"
ROOTFS_ROOT="$TOOLS_ROOT/rootfs"
ROOTFS_CURRENT="$ROOTFS_ROOT/current"
ROOTFS_STAMP=.alkera-rootfs
# The Ubuntu base the rootfs is built from, pinned with its published sha256
# per architecture (cdimage.ubuntu.com/ubuntu-base/releases/24.04/release).
ROOTFS_BASE_SERIES=24.04
ROOTFS_BASE_VERSION=24.04.4
ROOTFS_BASE_SHA256_AMD64=c1e67ef7b17a6300e136118bd1dc04725009cb376c1aad10abcf8cd453628d58
ROOTFS_BASE_SHA256_ARM64=04207713ece899c3740823d33690441ad3a7f0ded1101aca744e2b0f37ac7ff2
# What the agent's runtime needs from the OS inside the rootfs: TLS roots, git
# and curl for the model's own work, the archivers uv and pip reach for, and
# the tools a data agent expects a shell to have.
ROOTFS_PACKAGES="ca-certificates curl git bzip2 xz-utils unzip zip less procps file jq"
# Bump when the build steps below change in a way the inputs above do not show.
ROOTFS_RECIPE=2

log() { printf 'sandbox-prereqs: %s\n' "$*"; }

case "$HOST_FIREWALL" in
  required|optional) ;;
  *) echo "sandbox-prereqs: ALKERA_SANDBOX_HOST_FIREWALL must be required or optional, not '$HOST_FIREWALL'" >&2; exit 1 ;;
esac
case "$HOST_SCOPE" in
  owned|shared) ;;
  *) echo "sandbox-prereqs: ALKERA_SANDBOX_HOST_SCOPE must be owned or shared, not '$HOST_SCOPE'" >&2; exit 1 ;;
esac
if [ "$HOST_FIREWALL" = optional ] && [ "$SANDBOX_MODE" != none ]; then
  echo "sandbox-prereqs: the host firewall is never optional under $SANDBOX_MODE: the chat network is built on it" >&2
  exit 1
fi
# A host network step that failed. Fatal, unless the bootstrap declared the
# host firewall optional for this node — a none node in a container that
# cannot administer its network and has no metadata service to fence.
host_network_unavailable() { # what
  if [ "$HOST_FIREWALL" = optional ] && [ "$SANDBOX_MODE" = none ]; then
    log "$1 not applied: this container cannot administer its network; a none node with no metadata service runs without it"
    return 0
  fi
  echo "sandbox-prereqs: $1 failed; a $SANDBOX_MODE node needs the host firewall" >&2
  exit 1
}

if [ "$(id -u)" -ne 0 ]; then
  echo "sandbox-prereqs: run as root" >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64|amd64) ARCH=x86_64; DEB_ARCH=amd64; UV_TARGET=x86_64-unknown-linux-gnu; MAMBA_TARGET=linux-64 ;;
  aarch64|arm64) ARCH=aarch64; DEB_ARCH=arm64; UV_TARGET=aarch64-unknown-linux-gnu; MAMBA_TARGET=linux-aarch64 ;;
  *) echo "sandbox-prereqs: no sandbox tooling for $(uname -m)" >&2; exit 1 ;;
esac

# --- packages (the uid tools, acl, iproute2 and the firewall; both modes) -----
install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y --no-install-recommends uidmap nftables util-linux acl iproute2 bzip2 ca-certificates curl gnupg e2fsprogs quota
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y shadow-utils nftables util-linux acl iproute bzip2 ca-certificates curl e2fsprogs quota
  else
    echo "sandbox-prereqs: no apt-get or dnf; install uidmap/shadow-utils, nftables, util-linux, acl, iproute2, bzip2, ca-certificates, curl, e2fsprogs and quota by hand" >&2
    exit 1
  fi
}
if ! command -v nft >/dev/null 2>&1 || ! command -v setpriv >/dev/null 2>&1 || ! command -v setfacl >/dev/null 2>&1 \
   || ! command -v curl >/dev/null 2>&1 || ! command -v ip >/dev/null 2>&1 || ! command -v bzip2 >/dev/null 2>&1 \
   || ! command -v chattr >/dev/null 2>&1 || ! command -v setquota >/dev/null 2>&1; then
  install_packages
fi
log "nft $(nft --version | awk '{print $2}'), setpriv, setfacl, ip present"

# --- the org workers' id ranges (both modes) ---------------------------------
# Mapped by the supervisor (root) into each worker's user namespace through
# newuidmap/newgidmap, which admit only ranges listed here.
for ids in /etc/subuid /etc/subgid; do
  grep -q "^root:${ORG_UID_BASE}:" "$ids" 2>/dev/null || echo "root:${ORG_UID_BASE}:${ORG_UID_TOTAL}" >> "$ids"
done

# --- the checksum helpers -------------------------------------------------------
# Every download below is verified against a digest before it is used: a
# pinned constant where the publisher's checksums are stable, the publisher's
# own sidecar where a release moves (uv, micromamba, gVisor).
verify_sha256() { # file expected
  local got
  got="$(sha256sum "$1" | cut -d' ' -f1)"
  [ "$got" = "$2" ] || { echo "sandbox-prereqs: sha256 mismatch for $1 (got $got, want $2)" >&2; return 1; }
}
sidecar_digest() { # sidecar-file -> the first 64-hex (sha256) or 128-hex (sha512) token
  grep -oE '[0-9a-f]{128}|[0-9a-f]{64}' "$1" | head -n1
}

# --- uv, micromamba and a modern Python (both modes) --------------------------
# uv makes each chat's default environment and manages the interpreter; its
# own downloads of python-build-standalone are checked against the hashes it
# ships with. The host gets them so a "none" box can make the environment; a
# gvisor box copies the very same installs into the rootfs below, at the same
# paths, so an environment made on the host resolves inside the container.
mkdir -p "$TOOLS_ROOT" "$PYTHON_ROOT"
# The bootstrap runs this under umask 077, so a bare mkdir makes /opt/alkera
# 0700 and the chat's uid cannot traverse to the Python below it.
chmod 0755 "$TOOLS_ROOT" "$PYTHON_ROOT"
if ! command -v uv >/dev/null 2>&1; then
  uv_tmp="$(mktemp -d)"
  UV_URL="https://github.com/astral-sh/uv/releases/${UV_RELEASE}/download/uv-${UV_TARGET}.tar.gz"
  [ "$UV_RELEASE" = "latest" ] && UV_URL="https://github.com/astral-sh/uv/releases/latest/download/uv-${UV_TARGET}.tar.gz"
  curl -fsSL --retry 5 "$UV_URL" -o "$uv_tmp/uv.tar.gz"
  curl -fsSL --retry 5 "$UV_URL.sha256" -o "$uv_tmp/uv.tar.gz.sha256"
  verify_sha256 "$uv_tmp/uv.tar.gz" "$(sidecar_digest "$uv_tmp/uv.tar.gz.sha256")"
  tar -xzf "$uv_tmp/uv.tar.gz" -C "$uv_tmp"
  install -m 0755 -o root -g root "$uv_tmp/uv-${UV_TARGET}/uv" "$uv_tmp/uv-${UV_TARGET}/uvx" /usr/local/bin/
  rm -rf "$uv_tmp"
fi
log "uv $(uv --version 2>/dev/null | awk '{print $2}')"
if ! command -v micromamba >/dev/null 2>&1; then
  mm_tmp="$(mktemp -d)"
  MM_URL="https://github.com/mamba-org/micromamba-releases/releases/${MICROMAMBA_RELEASE}/download/micromamba-${MAMBA_TARGET}"
  [ "$MICROMAMBA_RELEASE" = "latest" ] && MM_URL="https://github.com/mamba-org/micromamba-releases/releases/latest/download/micromamba-${MAMBA_TARGET}"
  curl -fsSL --retry 5 "$MM_URL" -o "$mm_tmp/micromamba"
  curl -fsSL --retry 5 "$MM_URL.sha256" -o "$mm_tmp/micromamba.sha256"
  verify_sha256 "$mm_tmp/micromamba" "$(sidecar_digest "$mm_tmp/micromamba.sha256")"
  install -m 0755 -o root -g root "$mm_tmp/micromamba" /usr/local/bin/micromamba
  rm -rf "$mm_tmp"
fi
log "micromamba $(micromamba --version 2>/dev/null | head -n1)"
if ! "$PYTHON_HOME/bin/python3" -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
  UV_PYTHON_INSTALL_DIR="$PYTHON_ROOT" uv python install "$PYTHON_VERSION"
  found="$(UV_PYTHON_INSTALL_DIR="$PYTHON_ROOT" uv python find "$PYTHON_VERSION")"
  ln -sfn "$(dirname "$(dirname "$found")")" "$PYTHON_HOME"
fi
# World-readable, root-owned: every chat runs it, none may change it.
chmod -R a+rX "$PYTHON_ROOT"
log "python $("$PYTHON_HOME/bin/python3" --version 2>&1 | awk '{print $2}') at $PYTHON_HOME"
# The mount point a "none" box binds a chat's folder at (an empty root-owned
# directory otherwise); the rootfs carries its own.
mkdir -p "$SANDBOX_HOME"
chmod 0755 "$SANDBOX_HOME"

# --- gVisor (runsc), for a gvisor node only -----------------------------------
# From the gVisor apt repository, signed with the project's archive key, where
# there is apt; elsewhere the release tarball from the gVisor bucket, verified
# against its published sha512. A "none" node needs none of it.
if [ "$SANDBOX_MODE" = "gvisor" ]; then
  if ! command -v runsc >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      install -d -m 0755 /etc/apt/keyrings
      curl -fsSL --retry 5 https://gvisor.dev/archive.key | gpg --dearmor -o /etc/apt/keyrings/gvisor-archive-keyring.gpg
      chmod 0644 /etc/apt/keyrings/gvisor-archive-keyring.gpg
      echo "deb [arch=${DEB_ARCH} signed-by=/etc/apt/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" >/etc/apt/sources.list.d/gvisor.list
      apt-get update
      apt-get install -y --no-install-recommends runsc
    else
      RUNSC_URL="https://storage.googleapis.com/gvisor/releases/release/${RUNSC_RELEASE}/${ARCH}"
      runsc_tmp="$(mktemp -d)"
      curl -fsSL --retry 5 "${RUNSC_URL}/gvisor.tar.bz2" -o "$runsc_tmp/gvisor.tar.bz2"
      curl -fsSL --retry 5 "${RUNSC_URL}/gvisor.tar.bz2.sha512" -o "$runsc_tmp/gvisor.tar.bz2.sha512"
      ( cd "$runsc_tmp" && sha512sum -c gvisor.tar.bz2.sha512 )
      tar -xjf "$runsc_tmp/gvisor.tar.bz2" -C "$runsc_tmp" runsc containerd-shim-runsc-v1
      install -m 0755 -o root -g root "$runsc_tmp/runsc" "$runsc_tmp/containerd-shim-runsc-v1" /usr/local/bin/
      rm -rf "$runsc_tmp"
    fi
  fi
  log "runsc $(runsc --version 2>/dev/null | awk 'NR==1{print $NF}')"
fi

# --- the rootfs every container roots at (gvisor node only) --------------------
# Built once per recipe: the pinned Ubuntu base, the packages above installed
# inside it, and the host's own Python, uv and micromamba copied in at the same
# paths. The recipe digest names the build; a box whose current rootfs carries
# that digest in its stamp skips the build, and a change to any input builds a
# new one beside the old and moves `current` over. Never used half-built: the
# stamp is written last, the move is atomic.
if [ "$SANDBOX_MODE" = "gvisor" ]; then
  case "$DEB_ARCH" in
    amd64) ROOTFS_BASE_SHA256="$ROOTFS_BASE_SHA256_AMD64" ;;
    arm64) ROOTFS_BASE_SHA256="$ROOTFS_BASE_SHA256_ARM64" ;;
  esac
  python_build="$(basename "$(readlink -f "$PYTHON_HOME")")"
  recipe_digest="$(printf 'recipe=%s;base=%s;packages=%s;python=%s;uv=%s;micromamba=%s\n' \
    "$ROOTFS_RECIPE" "$ROOTFS_BASE_SHA256" "$ROOTFS_PACKAGES" "$python_build" \
    "$(uv --version 2>/dev/null | awk '{print $2}')" "$(micromamba --version 2>/dev/null | head -n1)" \
    | sha256sum | cut -d' ' -f1)"
  target="$ROOTFS_ROOT/$recipe_digest"
  mkdir -p "$ROOTFS_ROOT"
  # An org worker's uid reaches the staged rootfs by name to root its chats.
  chmod 0755 "$ROOTFS_ROOT"
  if [ -f "$target/$ROOTFS_STAMP" ] && [ "$(cat "$target/$ROOTFS_STAMP")" = "$recipe_digest" ]; then
    chmod 0755 "$target"
    log "rootfs $recipe_digest already staged"
  else
    log "staging rootfs $recipe_digest"
    work="$(mktemp -d -p "$ROOTFS_ROOT" .build.XXXXXX)"
    cleanup_rootfs_build() {
      umount "$work/proc" 2>/dev/null || true
      umount "$work/dev" 2>/dev/null || true
      rm -rf "$work"
    }
    trap cleanup_rootfs_build EXIT
    base_tar="$(mktemp)"
    ROOTFS_URL="https://cdimage.ubuntu.com/ubuntu-base/releases/${ROOTFS_BASE_SERIES}/release/ubuntu-base-${ROOTFS_BASE_VERSION}-base-${DEB_ARCH}.tar.gz"
    curl -fsSL --retry 5 "$ROOTFS_URL" -o "$base_tar"
    verify_sha256 "$base_tar" "$ROOTFS_BASE_SHA256"
    tar -xzf "$base_tar" -C "$work"
    rm -f "$base_tar"
    # The build needs a resolver and a /proc; the container later gets its own
    # resolv.conf bound over this one.
    if [ -s /run/systemd/resolve/resolv.conf ]; then
      cp /run/systemd/resolve/resolv.conf "$work/etc/resolv.conf"
    else
      cp /etc/resolv.conf "$work/etc/resolv.conf"
    fi
    mount --bind /proc "$work/proc"
    mount --bind /dev "$work/dev"
    chroot "$work" /usr/bin/env DEBIAN_FRONTEND=noninteractive /bin/bash -c \
      "apt-get update && apt-get install -y --no-install-recommends $ROOTFS_PACKAGES && apt-get clean && rm -rf /var/lib/apt/lists/*"
    umount "$work/dev"
    umount "$work/proc"
    mkdir -p "$work/opt/alkera" "$work/home/alkera" "$work/run/alkera"
    # Same umask: every directory the chat's uid must traverse is opened by hand.
    chmod 0755 "$work/opt/alkera" "$work/home/alkera" "$work/run/alkera"
    cp -a "$PYTHON_ROOT" "$work/opt/alkera/python"
    install -m 0755 -o root -g root /usr/local/bin/uv /usr/local/bin/uvx /usr/local/bin/micromamba "$work/usr/local/bin/"
    printf '%s\n' "$recipe_digest" >"$work/$ROOTFS_STAMP"
    chmod 0644 "$work/$ROOTFS_STAMP"
    # mktemp made the build directory private; the container's root must be
    # traversable by the chat's uid, as any root directory is.
    chmod 0755 "$work"
    mv "$work" "$target"
    trap - EXIT
    ln -sfn "$target" "$ROOTFS_CURRENT.tmp"
    mv -T "$ROOTFS_CURRENT.tmp" "$ROOTFS_CURRENT"
    # Older builds go; the one `current` names stays.
    for old in "$ROOTFS_ROOT"/*/; do
      old="${old%/}"
      [ "$old" = "$target" ] && continue
      case "$(basename "$old")" in .build.*|current) continue ;; esac
      rm -rf "$old"
    done
    log "rootfs staged at $target"
  fi
fi

# --- the chats' mountpoints in the staged rootfs (gvisor node only) -----------
# runsc makes a bind's mountpoint inside the shared rootfs when it is missing.
# An org worker roots its chats at the rootfs read-only and cannot, so every
# mountpoint a chat's binds land on is made here, once, as root: the agents'
# trees, and a workspace's kernel sandbox's platform mount and runtime.
if [ -d "$ROOTFS_CURRENT/" ]; then
  for mountpoint in /opt/alkera/harness/data/agent /opt/alkera/harness/state \
    /opt/alkera/harness/config /opt/alkera/envs /opt/alkera/agent /opt/alkera/rg \
    /opt/alkera/py /opt/alkera/run; do
    # Every directory on the way, not only the last: install -d makes a
    # missing parent under the umask, and this runs under the bootstrap's 077,
    # which left /opt/alkera/harness 0700 root and no chat uid able to reach
    # its state below it.
    path=""
    for part in $(printf '%s' "$mountpoint" | tr '/' ' '); do
      path="$path/$part"
      install -d -m 0755 "$ROOTFS_CURRENT$path"
      chmod 0755 "$ROOTFS_CURRENT$path"
    done
  done
fi

# --- the parent cgroup slice (both modes) -------------------------------------
have_systemd=0
if command -v systemctl >/dev/null 2>&1; then
  case "$(systemctl is-system-running 2>/dev/null || true)" in
    running|degraded|starting|maintenance) have_systemd=1 ;;
  esac
fi
if [ "$have_systemd" -eq 1 ]; then
  # Persistent (no --runtime): the per-chat slices live under it across reboots.
  systemctl set-property alkera.slice CPUAccounting=yes MemoryAccounting=yes TasksAccounting=yes
  log "alkera.slice accounted under systemd"
elif [ -w /sys/fs/cgroup ] && [ -e /sys/fs/cgroup/cgroup.controllers ]; then
  mkdir -p /sys/fs/cgroup/alkera.slice
  # Delegate the controllers the per-chat groups set limits on.
  for ctl in cpu memory pids; do
    if grep -qw "$ctl" /sys/fs/cgroup/cgroup.controllers; then
      echo "+$ctl" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true
      echo "+$ctl" > /sys/fs/cgroup/alkera.slice/cgroup.subtree_control 2>/dev/null || true
    fi
  done
  log "alkera.slice created under cgroupfs"
else
  log "no systemd and no writable cgroup v2: chats will run with no cgroup here"
fi

# --- forwarding for the chat namespaces (both modes) --------------------------
# A chat's namespace reaches out through the host, which has to forward for
# it. Persisted so a reboot keeps it; applied now so this boot has it.
mkdir -p "$(dirname "$SYSCTL_FILE")"
printf 'net.ipv4.ip_forward = 1\nnet.ipv4.conf.all.forwarding = 1\n' >"$SYSCTL_FILE"
sysctl -q -p "$SYSCTL_FILE" || sysctl -qw net.ipv4.ip_forward=1 || host_network_unavailable "forwarding"

# --- firewall (both modes) ----------------------------------------------------
# ssh's own port is always admitted, whatever it is, so a re-run can never lock
# the box out.
ssh_port="$(sshd -T 2>/dev/null | awk '/^port /{print $2}' | head -n1 || true)"
ports="$INBOUND_TCP_PORTS"
if [ -n "$ssh_port" ] && ! printf ' %s ' "$ports" | tr ',' ' ' | grep -q " $ssh_port "; then
  ports="$ports,$ssh_port"
fi
ports="$(printf '%s' "$ports" | tr ' ' ',' | sed 's/,,*/,/g; s/^,//; s/,$//')"

# What is not the node's: dropped on a host the plane owns, left to the host
# on one its operator shares (an nft comment there, so the ruleset still loads).
case "$HOST_SCOPE" in
  owned)
    host_inbound="jump host_inbound"
    host_forward="jump host_forward"
    ;;
  shared)
    host_inbound="# a shared host: inbound that is not the node's is the host's own"
    host_forward="# a shared host: forwarding that is not the node's is the host's own"
    ;;
esac

mkdir -p "$ENV_DIR"
# Readable by every uid (an org worker reads sandbox.env); what is secret here
# is 0600 on its own, and the node's credential is not here at all.
chmod 0755 "$ENV_DIR"
cat > "$NFT_FILE.tmp" <<EOF
#!/usr/sbin/nft -f
# Alkera chat sandbox: no chat uid reaches the metadata service; a chat's
# namespace reaches the daemon on the granted ports and nothing else on the
# host, never the metadata service, never another chat, and goes out through
# NAT. On a host the plane owns nothing new comes in but ssh and the ports
# named at provisioning, and nothing else is forwarded; on a shared host only
# the node's own links and uids are filtered.
# Chats and org workers reach no private, loopback, link-local or reserved
# address (egress_deny_*) but the entries the box fills in egress_allow_* at
# start (alkera_core.compute.box_egress): its backend, gateway and resolvers.
# Replaced whole on every run of sandbox-prereqs.sh.
table inet alkera_sandbox
flush table inet alkera_sandbox
table inet alkera_sandbox {
  set chat_ports {
    type ifname . inet_service
  }
  set egress_deny_v4 { type ipv4_addr; flags interval; elements = { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 168.63.129.16/32, 169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.0.2.0/24, 192.88.99.0/24, 192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24, 203.0.113.0/24, 224.0.0.0/3 } }
  set egress_deny_v6 { type ipv6_addr; flags interval; elements = { ::/127, 100::/64, 2001::/32, 2001:db8::/32, 2002::/16, fc00::/7, fe80::/10, ff00::/8 } }
  set egress_allow_v4 { type ipv4_addr . inet_proto . inet_service; }
  set egress_allow_v6 { type ipv6_addr . inet_proto . inet_service; }
  chain output {
    type filter hook output priority filter; policy accept;
    meta skuid ${CHAT_UID_MIN}-${CHAT_UID_MAX} ip daddr ${METADATA_IPV4} counter drop
    meta skuid ${CHAT_UID_MIN}-${CHAT_UID_MAX} ip6 daddr ${METADATA_IPV6} counter drop
    meta skuid ${CHAT_UID_MIN}-${CHAT_UID_MAX} oif "lo" accept
    meta skuid ${CHAT_UID_MIN}-${CHAT_UID_MAX} ip daddr . meta l4proto . th dport @egress_allow_v4 accept
    meta skuid ${CHAT_UID_MIN}-${CHAT_UID_MAX} ip6 daddr . meta l4proto . th dport @egress_allow_v6 accept
    meta skuid ${CHAT_UID_MIN}-${CHAT_UID_MAX} ip daddr @egress_deny_v4 counter drop
    meta skuid ${CHAT_UID_MIN}-${CHAT_UID_MAX} ip6 daddr @egress_deny_v6 counter drop
  }
  chain input {
    type filter hook input priority filter; policy accept;
    iif "lo" accept
    ct state established,related accept
    iifname . tcp dport @chat_ports accept
    iifname "vc*" counter drop
    iifname "vo*" counter drop
    ${host_inbound}
  }
  chain host_inbound {
    ct state invalid drop
    ip protocol icmp accept
    ip6 nexthdr ipv6-icmp accept
    tcp dport { ${ports} } accept
    counter drop
  }
  chain forward {
    type filter hook forward priority filter; policy accept;
    ct state established,related accept
    iifname "vc*" ct state invalid drop
    iifname "vo*" ct state invalid drop
    iifname "vc*" ip daddr ${METADATA_IPV4} counter drop
    iifname "vc*" ip6 daddr ${METADATA_IPV6} counter drop
    iifname "vc*" ip daddr ${SANDBOX_NET} counter drop
    iifname "vc*" ip daddr . meta l4proto . th dport @egress_allow_v4 accept
    iifname "vc*" ip6 daddr . meta l4proto . th dport @egress_allow_v6 accept
    iifname "vc*" ip daddr @egress_deny_v4 counter drop
    iifname "vc*" ip6 daddr @egress_deny_v6 counter drop
    iifname "vc*" oifname != "vc*" accept
    iifname "vc*" counter drop
    iifname "vo*" ip daddr ${METADATA_IPV4} counter drop
    iifname "vo*" ip6 daddr ${METADATA_IPV6} counter drop
    iifname "vo*" ip daddr ${ORG_NET} counter drop
    iifname "vo*" ip daddr ${SANDBOX_NET} counter drop
    iifname "vo*" ip daddr . meta l4proto . th dport @egress_allow_v4 accept
    iifname "vo*" ip6 daddr . meta l4proto . th dport @egress_allow_v6 accept
    iifname "vo*" ip daddr @egress_deny_v4 counter drop
    iifname "vo*" ip6 daddr @egress_deny_v6 counter drop
    iifname "vo*" oifname != "vo*" accept
    iifname "vo*" counter drop
    oifname "vc*" counter drop
    oifname "vo*" counter drop
    ${host_forward}
  }
  chain host_forward {
    counter drop
  }
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr ${SANDBOX_NET} oifname != "vc*" masquerade
    ip saddr ${ORG_NET} oifname != "vo*" masquerade
  }
}
EOF
mv "$NFT_FILE.tmp" "$NFT_FILE"
if nft -f "$NFT_FILE"; then
  # Reload on boot: nftables.service reads /etc/nftables.conf; include our file once.
  if [ -f /etc/nftables.conf ] && ! grep -qF "$NFT_FILE" /etc/nftables.conf; then
    printf '\ninclude "%s"\n' "$NFT_FILE" >> /etc/nftables.conf
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable nftables >/dev/null 2>&1 || true
  fi
  if [ "$HOST_SCOPE" = owned ]; then
    log "nftables: metadata blocked for uids ${CHAT_UID_MIN}-${CHAT_UID_MAX} and for ${SANDBOX_NET}; inbound tcp {${ports}} only"
  else
    log "nftables: metadata blocked for uids ${CHAT_UID_MIN}-${CHAT_UID_MAX} and for ${SANDBOX_NET}; a shared host: only the node's links are filtered"
  fi
else
  host_network_unavailable "the nftables ruleset"
fi

# --- the daemon's sandbox environment (both modes) ----------------------------
# Written once; an operator's later edits are kept.
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
# Chat sandbox settings the daemon reads.
ALKERA_SANDBOX_MODE=${SANDBOX_MODE}
ALKERA_SANDBOX_HOME=${SANDBOX_HOME}
ALKERA_SANDBOX_ROOTFS=${ROOTFS_CURRENT}
ALKERA_SANDBOX_PYTHON=${PYTHON_HOME}
ALKERA_SANDBOX_NET=${SANDBOX_NET}
SANDBOX_POOL_VCPU=1
SANDBOX_POOL_MEMORY_MB=2048
SANDBOX_DEDICATED_VCPU=8
SANDBOX_DEDICATED_MEMORY_MB=32768
EOF
  chmod 0644 "$ENV_FILE"
  log "wrote $ENV_FILE"
fi

# --- the readiness check, so the mode is known now ----------------------------
if [ "$SANDBOX_MODE" = "gvisor" ]; then
  if command -v runsc >/dev/null 2>&1 && runsc --version >/dev/null 2>&1 && [ -f "$ROOTFS_CURRENT/$ROOTFS_STAMP" ]; then
    log "mode gvisor: runsc is installed and runnable, rootfs staged"
  else
    log "mode gvisor but runsc is missing or broken, or no rootfs is staged: this box refuses every chat until it is fixed"
  fi
else
  log "mode none: no sandbox boundary (single-tenant box); per-chat uid and cgroup only"
fi
