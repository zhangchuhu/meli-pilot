#!/bin/sh
set -eu

for src in outputs/mexico-front-01.png outputs/mexico-front-02.png outputs/mexico-side-01.png outputs/mexico-back-01.png outputs/mexico-detail-01.png; do
  dst="${src%.png}-1200x1540.png"
  magick \
    \( "$src" -resize '1200x1540^' -gravity center -extent 1200x1540 -blur 0x28 \) \
    \( "$src" -resize '1200x1540' \) \
    -gravity center -compose over -composite -strip "$dst"
done
