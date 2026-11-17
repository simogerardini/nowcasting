#!/usr/bin/env bash
# Oracle Cloud Shell: retry launching the Always Free A1 instance until capacity is available.
# Zero cost: A1.Flex within Always Free limits only; aborts on any error other than "out of capacity".
# Usage (in Cloud Shell): bash oracle-launch-retry.sh      (keep the browser tab open)
set -uo pipefail

NAME=nowcast-vps
SUBNET_NAME="Public Subnet-nowcast-net"
SHAPE=VM.Standard.A1.Flex
CONFIGS=('{"ocpus":2,"memoryInGBs":12}' '{"ocpus":1,"memoryInGBs":6}')
SLEEP=75
MAX_HOURS=${MAX_HOURS:-12}
C=${OCI_TENANCY:?run this inside Oracle Cloud Shell}

PUB=~/nowcast.pub
if [ ! -s "$PUB" ]; then
  echo "!! $PUB missing: paste the content of ssh-key-*.key.pub into it, e.g.  nano ~/nowcast.pub"; exit 1
fi

cat > ~/nowcast-cloud-init.yaml <<'EOF'
#cloud-config
package_update: true
packages: [rsync, iptables-persistent, ca-certificates, curl]
runcmd:
  - iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp -m state --state NEW --dport 80 -j ACCEPT
  - iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp -m state --state NEW --dport 443 -j ACCEPT
  - netfilter-persistent save
  - '[ -f /swapfile ] || (fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo "/swapfile none swap sw 0 0" >> /etc/fstab)'
  - curl -fsSL https://get.docker.com | sh
  - systemctl enable --now docker
  - usermod -aG docker ubuntu
  - mkdir -p /opt/nowcast
EOF

AD=$(oci iam availability-domain list -c "$C" --query 'data[0].name' --raw-output)
SUBNET=$(oci network subnet list -c "$C" --all --query "data[?\"display-name\"=='$SUBNET_NAME'].id | [0]" --raw-output)
IMG=$(oci compute image list -c "$C" --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
      --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC --query 'data[0].id' --raw-output)
echo "AD=$AD"; echo "SUBNET=$SUBNET"; echo "IMAGE=$IMG"
[ -z "$SUBNET" ] || [ "$SUBNET" = "null" ] && { echo "!! subnet '$SUBNET_NAME' not found"; exit 1; }
[ -z "$IMG" ] || [ "$IMG" = "null" ] && { echo "!! Ubuntu 24.04 aarch64 image not found"; exit 1; }

show_ip() {
  oci compute instance list-vnics --instance-id "$1" --query 'data[0]."public-ip"' --raw-output
}

deadline=$(( $(date +%s) + MAX_HOURS * 3600 ))
i=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  # Never create a second instance
  EXIST=$(oci compute instance list -c "$C" --display-name "$NAME" --all \
          --query "data[?\"lifecycle-state\"!='TERMINATED' && \"lifecycle-state\"!='TERMINATING'].id | [0]" --raw-output 2>/dev/null)
  if [ -n "$EXIST" ] && [ "$EXIST" != "null" ]; then
    echo "== $NAME already exists: $EXIST  PUBLIC IP: $(show_ip "$EXIST")"; exit 0
  fi
  CFG=${CONFIGS[$(( i % ${#CONFIGS[@]} ))]}
  i=$((i + 1))
  echo "[$(date +%H:%M:%S)] attempt $i shape-config $CFG"
  OUT=$(oci compute instance launch -c "$C" --availability-domain "$AD" --display-name "$NAME" \
        --shape "$SHAPE" --shape-config "$CFG" --image-id "$IMG" --subnet-id "$SUBNET" \
        --assign-public-ip true --boot-volume-size-in-gbs 100 \
        --ssh-authorized-keys-file "$PUB" --user-data-file ~/nowcast-cloud-init.yaml \
        --wait-for-state RUNNING --max-wait-seconds 600 2>&1)
  if echo "$OUT" | grep -q '"lifecycle-state": "RUNNING"'; then
    ID=$(echo "$OUT" | grep -m1 '"id": "ocid1.instance' | sed 's/.*"\(ocid1[^"]*\)".*/\1/')
    echo "== CREATED $ID ($CFG)  PUBLIC IP: $(show_ip "$ID")"; exit 0
  elif echo "$OUT" | grep -qiE 'out of (host )?capacity'; then
    sleep "$SLEEP"
  elif echo "$OUT" | grep -qiE 'TooManyRequests|429'; then
    sleep 300
  else
    echo "!! unexpected error, stopping (nothing created):"; echo "$OUT" | tail -20; exit 1
  fi
done
echo "!! no capacity within ${MAX_HOURS}h"; exit 2
