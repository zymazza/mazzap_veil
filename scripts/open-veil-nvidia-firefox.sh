#!/usr/bin/env bash
set -euo pipefail

# Open VEIL in a dedicated Firefox instance rendered on the RTX 4090 via PRIME
# render offload. We intentionally use --no-remote/--new-instance because
# `firefox --new-tab` hands the URL to any existing Firefox process, and that
# existing process keeps whatever GPU it started with (currently the AMD iGPU).
#
# By default, focus the HDMI-A-3 Hyprland monitor before launching so the VEIL
# window appears there. Override with either:
#   VEIL_TARGET_MONITOR=DP-7 scripts/open-veil-nvidia-firefox.sh <url>
#   scripts/open-veil-nvidia-firefox.sh <url> DP-7
# Disable monitor targeting with VEIL_TARGET_MONITOR=none.
URL="${1:-http://127.0.0.1:4173/}"
TARGET_MONITOR="${2:-${VEIL_TARGET_MONITOR:-HDMI-A-3}}"
PROFILE_DIR="${VEIL_FIREFOX_PROFILE:-${XDG_CACHE_HOME:-$HOME/.cache}/veil-firefox-nvidia-profile}"
LOG_FILE="${VEIL_FIREFOX_LOG:-/tmp/veil-firefox-nvidia.log}"
DRY_RUN="${VEIL_DRY_RUN:-0}"

mkdir -p "$PROFILE_DIR"
cat > "$PROFILE_DIR/user.js" <<'PREFS'
user_pref("gfx.webrender.all", true);
user_pref("gfx.webrender.enabled", true);
user_pref("layers.acceleration.force-enabled", true);
user_pref("webgl.force-enabled", true);
user_pref("webgl.disabled", false);
PREFS

if [[ -n "$TARGET_MONITOR" && "$TARGET_MONITOR" != "none" ]]; then
  if command -v hyprctl >/dev/null 2>&1; then
    if hyprctl monitors -j 2>/dev/null | grep -Fq "\"name\": \"$TARGET_MONITOR\""; then
      if [[ "$DRY_RUN" == "1" ]]; then
        echo "DRY RUN: would focus Hyprland monitor $TARGET_MONITOR before launch"
      else
        hyprctl dispatch focusmonitor "$TARGET_MONITOR" >/dev/null || \
          echo "Warning: failed to focus Hyprland monitor $TARGET_MONITOR; launching anyway" >&2
      fi
    else
      echo "Warning: target monitor $TARGET_MONITOR not found by hyprctl; launching on current monitor" >&2
    fi
  else
    echo "Warning: hyprctl not available; cannot target monitor $TARGET_MONITOR" >&2
  fi
fi

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'DRY RUN: would launch: '
  printf '%q ' env \
    __NV_PRIME_RENDER_OFFLOAD=1 \
    __GLX_VENDOR_LIBRARY_NAME=nvidia \
    __VK_LAYER_NV_optimus=NVIDIA_only \
    DRI_PRIME=1 \
    MOZ_ENABLE_WAYLAND=1 \
    firefox --no-remote --new-instance --profile "$PROFILE_DIR" "$URL"
  printf '\n'
  echo "Profile: $PROFILE_DIR"
  echo "Log: $LOG_FILE"
  exit 0
fi

nohup env \
  __NV_PRIME_RENDER_OFFLOAD=1 \
  __GLX_VENDOR_LIBRARY_NAME=nvidia \
  __VK_LAYER_NV_optimus=NVIDIA_only \
  DRI_PRIME=1 \
  MOZ_ENABLE_WAYLAND=1 \
  firefox --no-remote --new-instance --profile "$PROFILE_DIR" "$URL" \
  >"$LOG_FILE" 2>&1 </dev/null &

echo "Opened $URL in NVIDIA-offloaded Firefox profile: $PROFILE_DIR"
echo "Target monitor: ${TARGET_MONITOR:-current}"
echo "Log: $LOG_FILE"
