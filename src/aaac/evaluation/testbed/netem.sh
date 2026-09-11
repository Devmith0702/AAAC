#!/bin/sh
# ---------------------------------------------------------------------------
# Apply the per-class qdisc chain inside a client container (CLAUDE.md §4.2).
# Requires NET_ADMIN. Run once at container start, before any load.
#
# WHY THIS SHAPES THE INGRESS PATH BY DEFAULT
# -------------------------------------------
# §4.2 describes egress shaping and calls ingress shaping optional, "if
# downstream asymmetry matters". For this experiment it is not optional. The
# thing a LOW-class student cannot finish inside their admission window is a
# ~450 KB *download* (§2). A tbf on the container's egress caps what the client
# uploads and leaves the download running at full VM speed, so the LOW class
# would not be slow, the completion gap would not appear, and the whole testbed
# would silently measure nothing. Downstream shaping is therefore the default
# and the delay/jitter/loss are applied on that path, where the data actually
# travels.
#
# Delay is applied on one path only, so measured RTT ~= the configured delay,
# which is what the §4.2 verification gate asserts. AAAC_NETEM_DIRECTION=both
# splits the delay across the two paths to keep that identity.
#
# A `tc` command that silently no-ops looks exactly like one that worked, so
# every step is checked and the script exits non-zero on any mismatch.
# ---------------------------------------------------------------------------
set -eu

IFACE="${AAAC_NETEM_IFACE:-eth0}"
IFB="${AAAC_NETEM_IFB:-ifb0}"
PROFILE="${AAAC_NETEM_PROFILE:-UNSET}"
RATE="${AAAC_NETEM_RATE:?AAAC_NETEM_RATE is required}"
DELAY="${AAAC_NETEM_DELAY:?AAAC_NETEM_DELAY is required}"
JITTER="${AAAC_NETEM_JITTER:?AAAC_NETEM_JITTER is required}"
LOSS="${AAAC_NETEM_LOSS:?AAAC_NETEM_LOSS is required}"
BURST="${AAAC_NETEM_BURST:-32kbit}"
LATENCY="${AAAC_NETEM_LATENCY:-400ms}"
DIRECTION="${AAAC_NETEM_DIRECTION:-ingress}"

die() {
    echo "netem.sh: FATAL: $*" >&2
    exit 1
}

need() {
    command -v "$1" >/dev/null 2>&1 || die "missing '$1' in the image (need iproute2)"
}

need tc
need ip

case "$DIRECTION" in
ingress | egress | both) ;;
*) die "AAAC_NETEM_DIRECTION must be ingress, egress or both (got '$DIRECTION')" ;;
esac

# Halve the delay when shaping both paths so RTT still matches the profile.
half_delay() {
    echo "$1" | awk '{
        v = $0; sub(/ms$/, "", v);
        printf("%gms", v / 2)
    }'
}

if [ "$DIRECTION" = "both" ]; then
    PATH_DELAY="$(half_delay "$DELAY")"
    PATH_JITTER="$(half_delay "$JITTER")"
else
    PATH_DELAY="$DELAY"
    PATH_JITTER="$JITTER"
fi

# --- clear anything left from a previous run -------------------------------
tc qdisc del dev "$IFACE" root >/dev/null 2>&1 || true
tc qdisc del dev "$IFACE" ingress >/dev/null 2>&1 || true
ip link del "$IFB" >/dev/null 2>&1 || true

# --- egress (client -> server) ---------------------------------------------
tc qdisc add dev "$IFACE" root handle 1: tbf \
    rate "$RATE" burst "$BURST" latency "$LATENCY" ||
    die "could not attach tbf to $IFACE (is NET_ADMIN granted?)"

if [ "$DIRECTION" = "egress" ] || [ "$DIRECTION" = "both" ]; then
    tc qdisc add dev "$IFACE" parent 1:1 handle 10: netem \
        delay "$PATH_DELAY" "$PATH_JITTER" distribution normal loss "$LOSS" ||
        die "could not attach netem to $IFACE"
fi

# --- ingress (server -> client), redirected through an ifb device ----------
if [ "$DIRECTION" = "ingress" ] || [ "$DIRECTION" = "both" ]; then
    ip link add "$IFB" type ifb ||
        die "could not create $IFB. The ifb kernel module is unavailable in this
    kernel (common under Docker Desktop). Downstream shaping is REQUIRED for a
    valid run - see the header of this script. Load ifb on the host, or re-run
    with AAAC_NETEM_DIRECTION=egress and record in the report that downstream
    was unshaped and the result is therefore not a valid measurement of the
    completion gap."
    ip link set "$IFB" up || die "could not bring $IFB up"

    tc qdisc add dev "$IFACE" handle ffff: ingress ||
        die "could not attach the ingress qdisc to $IFACE"
    tc filter add dev "$IFACE" parent ffff: protocol all u32 match u32 0 0 \
        action mirred egress redirect dev "$IFB" ||
        die "could not redirect $IFACE ingress to $IFB"

    tc qdisc add dev "$IFB" root handle 1: tbf \
        rate "$RATE" burst "$BURST" latency "$LATENCY" ||
        die "could not attach tbf to $IFB"
    tc qdisc add dev "$IFB" parent 1:1 handle 10: netem \
        delay "$PATH_DELAY" "$PATH_JITTER" distribution normal loss "$LOSS" ||
        die "could not attach netem to $IFB"
fi

# --- prove it actually took --------------------------------------------------
tc qdisc show dev "$IFACE" | grep -q 'tbf' ||
    die "tbf is not present on $IFACE after apply - the shaping silently no-opped"

if [ "$DIRECTION" = "ingress" ] || [ "$DIRECTION" = "both" ]; then
    tc qdisc show dev "$IFB" | grep -q 'netem' ||
        die "netem is not present on $IFB after apply - the shaping silently no-opped"
fi

if [ "$DIRECTION" = "egress" ] || [ "$DIRECTION" = "both" ]; then
    tc qdisc show dev "$IFACE" | grep -q 'netem' ||
        die "netem is not present on $IFACE after apply - the shaping silently no-opped"
fi

cat <<EOF
netem.sh: applied profile ${PROFILE} on ${IFACE} (direction=${DIRECTION})
  rate    ${RATE}   burst ${BURST}   latency ${LATENCY}
  delay   ${PATH_DELAY} +/- ${PATH_JITTER} (normal)
  loss    ${LOSS}
--- qdisc ${IFACE} ---
$(tc qdisc show dev "$IFACE")
EOF

if [ "$DIRECTION" = "ingress" ] || [ "$DIRECTION" = "both" ]; then
    echo "--- qdisc ${IFB} ---"
    tc qdisc show dev "$IFB"
fi
