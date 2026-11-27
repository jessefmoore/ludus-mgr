#!/usr/bin/env bash
#
# make-demo-media.sh — turn a screen recording into repo-friendly demo media.
#
# Produces two things from one input video:
#   1. A compressed, web-optimized MP4  -> drag-drop into a GitHub issue/README
#      to get a github.com/user-attachments URL (video is NOT committed to git).
#   2. A small, palette-optimized looping GIF -> commit into docs/ and embed
#      inline with ![walkthrough](docs/walkthrough.gif) (autoplays on GitHub).
#
# Usage:
#   scripts/make-demo-media.sh RECORDING.mp4 [options]
#
# Options:
#   -o, --out-dir DIR       Output directory       (default: same dir as input)
#       --name NAME         Output basename        (default: input filename stem)
#       --mp4-width PX       MP4 max width          (default: 1280)
#       --mp4-crf N          MP4 quality, lower=better/bigger (default: 28)
#       --mute               Strip audio from the MP4 (default: keep audio)
#       --gif-start TS       GIF slice start, e.g. 5 or 00:00:05 (default: whole clip)
#       --gif-duration SEC   GIF slice length in seconds        (default: whole clip)
#       --gif-width PX       GIF width              (default: 900)
#       --gif-fps N          GIF frame rate         (default: 12)
#       --no-gif             Skip the GIF, only make the MP4
#       --no-mp4             Skip the MP4, only make the GIF
#   -h, --help              Show this help
#
# Examples:
#   # both outputs, GIF from a 10s slice starting at 0:05
#   scripts/make-demo-media.sh ~/dashboard-walkthrough.mp4 \
#       --gif-start 5 --gif-duration 10 --out-dir docs --name walkthrough
#
#   # just a compressed MP4 for uploading, keep audio
#   scripts/make-demo-media.sh ~/rec.mp4 --no-gif

set -euo pipefail

# ── defaults ────────────────────────────────────────────────────────────────
OUT_DIR=""
NAME=""
MP4_WIDTH=1280
MP4_CRF=28
MP4_PRESET=veryfast
KEEP_AUDIO=1
GIF_START=""
GIF_DURATION=""
GIF_WIDTH=900
GIF_FPS=12
MAKE_GIF=1
MAKE_MP4=1

die() { echo "error: $*" >&2; exit 1; }

command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg not found (sudo apt install ffmpeg)"

# ── parse args ──────────────────────────────────────────────────────────────
INPUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--out-dir)      OUT_DIR="$2"; shift 2 ;;
    --name)            NAME="$2"; shift 2 ;;
    --mp4-width)       MP4_WIDTH="$2"; shift 2 ;;
    --mp4-crf)         MP4_CRF="$2"; shift 2 ;;
    --mute)            KEEP_AUDIO=0; shift ;;
    --gif-start)       GIF_START="$2"; shift 2 ;;
    --gif-duration)    GIF_DURATION="$2"; shift 2 ;;
    --gif-width)       GIF_WIDTH="$2"; shift 2 ;;
    --gif-fps)         GIF_FPS="$2"; shift 2 ;;
    --no-gif)          MAKE_GIF=0; shift ;;
    --no-mp4)          MAKE_MP4=0; shift ;;
    -h|--help)         sed -n '2,40p' "$0"; exit 0 ;;
    -*)                die "unknown option: $1" ;;
    *)                 [ -z "$INPUT" ] && INPUT="$1" || die "unexpected arg: $1"; shift ;;
  esac
done

[ -n "$INPUT" ]    || die "no input video given (see --help)"
[ -f "$INPUT" ]    || die "input not found: $INPUT"

# derive output naming
[ -n "$OUT_DIR" ] || OUT_DIR="$(dirname "$INPUT")"
[ -n "$NAME" ]    || NAME="$(basename "${INPUT%.*}")"
mkdir -p "$OUT_DIR"

MP4_OUT="$OUT_DIR/${NAME}-small.mp4"
GIF_OUT="$OUT_DIR/${NAME}.gif"

# human-readable size helper
human_size() { du -h "$1" 2>/dev/null | cut -f1; }

# scale filter that never upscales, keeps even dimensions (required by yuv420p)
mp4_scale="scale='min(${MP4_WIDTH},iw)':-2"

# ── 1. compressed MP4 ───────────────────────────────────────────────────────
if [ "$MAKE_MP4" -eq 1 ]; then
  echo "▶ encoding MP4  -> $MP4_OUT"
  if [ "$KEEP_AUDIO" -eq 1 ]; then
    audio_args=(-c:a aac -b:a 128k)
  else
    audio_args=(-an)
  fi
  ffmpeg -y -loglevel error -stats -i "$INPUT" \
    -vf "$mp4_scale" \
    -c:v libx264 -crf "$MP4_CRF" -preset "$MP4_PRESET" -pix_fmt yuv420p \
    -movflags +faststart \
    "${audio_args[@]}" \
    "$MP4_OUT"
  echo "  ✓ $MP4_OUT  ($(human_size "$MP4_OUT"))"
fi

# ── 2. palette-optimized GIF (two-pass for quality at small size) ────────────
if [ "$MAKE_GIF" -eq 1 ]; then
  echo "▶ encoding GIF  -> $GIF_OUT"
  # optional slice
  slice_args=()
  [ -n "$GIF_START" ]    && slice_args+=(-ss "$GIF_START")
  [ -n "$GIF_DURATION" ] && slice_args+=(-t "$GIF_DURATION")

  palette="$(mktemp --suffix=.png)"
  trap 'rm -f "$palette"' EXIT

  gif_filters="fps=${GIF_FPS},scale=${GIF_WIDTH}:-1:flags=lanczos"

  # pass 1: build an optimal color palette from the (sliced) frames
  ffmpeg -y -loglevel error "${slice_args[@]}" -i "$INPUT" \
    -vf "${gif_filters},palettegen=stats_mode=diff" "$palette"

  # pass 2: render the GIF using that palette
  ffmpeg -y -loglevel error -stats "${slice_args[@]}" -i "$INPUT" -i "$palette" \
    -lavfi "${gif_filters}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
    "$GIF_OUT"

  echo "  ✓ $GIF_OUT  ($(human_size "$GIF_OUT"))"
  gif_bytes=$(stat -c%s "$GIF_OUT")
  if [ "$gif_bytes" -gt $((5 * 1024 * 1024)) ]; then
    echo "  ⚠ GIF is >5 MB — trim it with --gif-start/--gif-duration, or lower"
    echo "    --gif-width / --gif-fps, before committing it to the repo."
  fi
fi

echo
echo "Done."
[ "$MAKE_MP4" -eq 1 ] && echo "  • MP4: drag '$MP4_OUT' into a GitHub issue/README to get a hosted URL."
[ "$MAKE_GIF" -eq 1 ] && echo "  • GIF: commit '$GIF_OUT' and embed:  ![walkthrough]($GIF_OUT)"
