#!/usr/bin/env bash
# Oracle Cloud Shell: retry creating ONE Always Free instance until capacity frees up.
# Alternates VM.Standard.E2.1.Micro (AMD, 1 GB) and VM.Standard.A1.Flex (ARM, 2 OCPU/12 GB, then 1/6).
# Stops on success, if the instance already exists, or on any error other than capacity/rate limits.
# Usage in Cloud Shell:  bash oracle-launch-retry.sh     (keep the tab open)
set -uo pipefail
NAME=nowcast-vps
SUBNET_NAME="Public Subnet-nowcast-net"
SLEEP=75
MAX_HOURS=${MAX_HOURS:-24}
C=${OCI_TENANCY:?run inside Oracle Cloud Shell}

# SSH key: reuse ~/nowcast.pub if uploaded, else generate a pair in Cloud Shell
if [ ! -s ~/nowcast.pub ]; then
  [ -f ~/nowcast_key ] || ssh-keygen -t ed25519 -N "" -C nowcast -f ~/nowcast_key -q
  cp ~/nowcast_key.pub ~/nowcast.pub
  echo ">> new SSH key generated: private key = ~/nowcast_key (copy it with: cat ~/nowcast_key)"
fi

cat > ~/nowcast-cloud-init.yaml <<'EOF'
#cloud-config
package_update: true
packages: [rsync, iptables-persistent, ca-certificates, curl]
runcmd:
  - '[ -f /swapfile ] || (fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo "/swapfile none swap sw 0 0" >> /etc/fstab)'
  - iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp -m state --state NEW --dport 80 -j ACCEPT
  - iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp -m state --state NEW --dport 443 -j ACCEPT
  - netfilter-persistent save
  - curl -fsSL https://get.docker.com | sh
  - systemctl enable --now docker
  - mkdir -p /opt/nowcast
EOF

AD=$(oci iam availability-domain list -c "$C" --query 'data[0].name' --raw-output)
SUBNET=$(oci network subnet list -c "$C" --all --query "data[?\"display-name\"=='$SUBNET_NAME'].id | [0]" --raw-output)
{ [ -z "$SUBNET" ] || [ "$SUBNET" = "null" ]; } && { echo "!! subnet '$SUBNET_NAME' not found"; exit 1; }

image_for() {  # newest Ubuntu 24.04 (Minimal preferred) compatible with the shape
  local id
  for ver in "24.04 Minimal" "24.04"; do
    id=$(oci compute image list -c "$C" --operating-system "Canonical Ubuntu" --operating-system-version "$ver" \
         --shape "$1" --sort-by TIMECREATED --sort-order DESC --query 'data[0].id' --raw-output 2>/dev/null)
    [ -n "$id" ] && [ "$id" != "null" ] && { echo "$id"; return; }
  done
}
IMG_MICRO=$(image_for VM.Standard.E2.1.Micro)
IMG_A1=$(image_for VM.Standard.A1.Flex)
echo "AD=$AD"; echo "SUBNET=$SUBNET"; echo "IMG_MICRO=$IMG_MICRO"; echo "IMG_A1=$IMG_A1"

show_ip() { oci compute instance list-vnics --instance-id "$1" --query 'data[0]."public-ip"' --raw-output; }

ATTEMPTS=("micro" "a1:2:12" "micro" "a1:1:6")
deadline=$(( $(date +%s) + MAX_HOURS * 3600 )); i=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  EXIST=$(oci compute instance list -c "$C" --display-name "$NAME" --all \
    --query "data[?\"lifecycle-state\"!='TERMINATED' && \"lifecycle-state\"!='TERMINATING'].id | [0]" --raw-output 2>/dev/null)
  if [ -n "$EXIST" ] && [ "$EXIST" != "null" ]; then
    echo "== $NAME already exists: $EXIST  PUBLIC IP: $(show_ip "$EXIST")"; exit 0
  fi
  A=${ATTEMPTS[$(( i % ${#ATTEMPTS[@]} ))]}; i=$((i + 1))
  if [ "$A" = micro ]; then
    [ -z "$IMG_MICRO" ] && continue
    ARGS=(--shape VM.Standard.E2.1.Micro --image-id "$IMG_MICRO")
  else
    IFS=: read -r _ OCPU MEM <<< "$A"
    [ -z "$IMG_A1" ] && continue
    ARGS=(--shape VM.Standard.A1.Flex --shape-config "{\"ocpus\":$OCPU,\"memoryInGBs\":$MEM}" --image-id "$IMG_A1")
  fi
  echo "[$(date +%H:%M:%S)] attempt $i: $A"
  OUT=$(oci compute instance launch -c "$C" --availability-domain "$AD" --display-name "$NAME" "${ARGS[@]}" \
        --subnet-id "$SUBNET" --assign-public-ip true \
        --ssh-authorized-keys-file ~/nowcast.pub --user-data-file ~/nowcast-cloud-init.yaml \
        --wait-for-state RUNNING --max-wait-seconds 600 2>&1)
  if echo "$OUT" | grep -q '"lifecycle-state": "RUNNING"'; then
    ID=$(echo "$OUT" | grep -m1 '"id": "ocid1.instance' | sed 's/.*"\(ocid1[^"]*\)".*/\1/')
    echo "== CREATED $ID ($A)  PUBLIC IP: $(show_ip "$ID")"; exit 0
  elif echo "$OUT" | grep -qiE 'out of (host )?capacity'; then
    sleep "$SLEEP"
  elif echo "$OUT" | grep -qiE 'TooManyRequests|"status": 429'; then
    sleep 300
  else
    echo "!! unexpected error, stopping (nothing created):"; echo "$OUT" | tail -20; exit 1
  fi
done
echo "!! no capacity within ${MAX_HOURS}h"; exit 2
