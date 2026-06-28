#!/usr/bin/env python3
"""
ghibli_scenes — 8-bit Ghibli-inspired battle scene renderer for Stream Deck Plus.

Renders a low-resolution (120x60) pixel-art scene that spans the full 4x2 key
grid as one continuous canvas (480x240 when scaled 4x with NEAREST). The scene
depicts "Laputa Siege at Golden Hour": a floating fortress under lightning
attack at sunset, with parallax mountains, ember particles, and patrolling
airships.

The renderer is pure: render_scene(phase_seconds) -> PIL RGB image at 120x60.
All motion is derived from `phase` (a monotonic seconds float), so the same
phase always produces the same frame — no per-call randomness that would make
caching impossible.

Scene composition (back → front):
  1. Banded sunset sky gradient (animated hue shift)
  2. Large pixel sun, slowly descending with halo glow
  3. Warm-lit cloud layer (parallax slow)
  4. Distant mountain silhouettes (parallax medium-slow)
  5. Floating Laputa fortress: blocky silhouette + glowing windows (bobbing)
  6. Energy shield: concentric pulse around fortress
  7. Mid-range mountains (parallax medium-fast)
  8. Lightning bolt: jagged cloud→fortress strike (periodic, hash-driven)
  9. Forest tree line (darkest silhouette, minimal parallax)
 10. Rising ember particles (deterministic pseudo-random paths)
 11. Patrolling airship silhouette (crosses periodically)

ponytail: hand-drawn sprites via filled rectangles/polygons, no external assets.
         upgrade: sprite sheet PNGs if we want movie-accurate Laputa silhouettes.
"""
import math
from PIL import Image, ImageDraw

# ---- canvas geometry -------------------------------------------------------
# Internal 8-bit resolution. Scaled 4x to 480x240 (4 keys x 120px wide,
# 2 keys x 120px tall) with Image.NEAREST for crisp chunky pixels.
SCENE_W, SCENE_H = 120, 60
SCALE = 4
CANVAS_W, CANVAS_H = SCENE_W * SCALE, SCENE_H * SCALE  # 480 x 240

# ---- Ghibli sunset palette (Laputa siege) ---------------------------------
# Constrained to ~16 hues for an authentic NES-era feel. Warm golden-hour base
# with cool indigo shadows — the Ghibli signature tension.
PAL = {
    "sky_zenith":  (18, 10, 42),     # deep indigo (top of sky)
    "sky_purple":  (55, 20, 68),     # purple band
    "sky_magenta": (120, 35, 65),    # magenta transition
    "sky_orange":  (200, 75, 30),    # warm orange
    "sky_gold":    (240, 170, 50),   # golden band
    "sky_horizon": (255, 220, 110),  # pale gold (where sun sits)
    "sun_core":    (255, 245, 180),  # bright sun disc
    "sun_glow":    (255, 190, 70),   # sun halo
    "cloud_hi":    (230, 140, 55),   # sun-lit cloud edge
    "cloud_lo":    (50, 25, 50),     # cloud shadow
    "mtn_far":     (40, 20, 48),     # most distant ridge
    "mtn_mid":     (22, 10, 32),     # mid ridge
    "mtn_near":    (10, 5, 18),      # nearest ridge / forest
    "fortress":    (8, 5, 15),       # Laputa silhouette
    "window":      (255, 210, 90),   # glowing fortress windows
    "shield":      (100, 200, 235),  # energy shield cyan
    "lightning":   (235, 245, 255),  # electric white-blue
    "ember":       (255, 130, 35),   # rising spark
    "airship":     (12, 8, 22),      # airship hull
    "banner":      (255, 200, 100),  # touchscreen banner text
}

HORIZON_Y = int(SCENE_H * 0.72)  # where sky meets land (~y=43)


# ---- helpers ---------------------------------------------------------------
def _lerp(a, b, t):
    t = max(0.0, min(1.0, t))
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))

def _ease(t):
    """Smoothstep: 0→1 with eased acceleration."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)

def _hash(n):
    """Deterministic pseudo-random in [0,1) from an integer seed.
    Used for lightning timing and ember spawning — no per-frame RNG."""
    return ((math.sin(n * 127.1 + 311.7) * 43758.5453) % 1.0 + 1.0) % 1.0

def _triangular_ridge(draw, base_y, amplitude, period, offset, color, w):
    """Draw a zig-zag mountain ridge as a filled polygon spanning the full width.
    `period` = peak-to-peak width in internal pixels; `offset` = horizontal scroll."""
    pts = [(0, SCENE_H)]
    x = -int(offset % period)
    i = 0
    while x <= w + period:
        # Alternating peak/valley heights for a natural ridge line
        h = base_y - int(amplitude * (0.6 + 0.4 * _hash(int((x + offset) / period) + i)))
        pts.append((x, h))
        pts.append((x + period // 2, base_y - int(amplitude * 0.3)))
        x += period
        i += 1
    pts.append((w, SCENE_H))
    draw.polygon(pts, fill=color)

def _pixel_circle(px, cx, cy, r, color, w, h):
    """Draw an aliased filled circle by testing each pixel. Used for the sun
    and shield pulses where ImageDraw.ellipse anti-aliasing would look wrong."""
    r2 = r * r
    x0 = max(0, int(cx - r))
    x1 = min(w - 1, int(cx + r))
    y0 = max(0, int(cy - r))
    y1 = min(h - 1, int(cy + r))
    for y in range(y0, y1 + 1):
        dy = y - cy
        for x in range(x0, x1 + 1):
            dx = x - cx
            if dx * dx + dy * dy <= r2:
                px[x, y] = color

def _pixel_circle_blend(px, cx, cy, r, color, blend, w, h):
    """Circle with per-pixel alpha blend onto existing pixels (for glows)."""
    r2 = r * r
    x0 = max(0, int(cx - r))
    x1 = min(w - 1, int(cx + r))
    y0 = max(0, int(cy - r))
    y1 = min(h - 1, int(cy + r))
    for y in range(y0, y1 + 1):
        dy = y - cy
        for x in range(x0, x1 + 1):
            dx = x - cx
            d2 = dx * dx + dy * dy
            if d2 <= r2:
                # Radial falloff: strongest at center, fades at edge
                falloff = (1.0 - math.sqrt(d2) / r) * blend
                px[x, y] = _lerp(px[x, y], color, falloff)


# ---- scene renderer --------------------------------------------------------
def render_scene(phase):
    """Render one frame of the Laputa siege scene at 120x60.
    `phase` = monotonic seconds. Returns PIL.Image RGB."""
    img = Image.new("RGB", (SCENE_W, SCENE_H), PAL["sky_zenith"])
    px = img.load()
    draw = ImageDraw.Draw(img)

    # --- Layer 1: banded sunset sky ---
    # Slow hue drift over 90s — the sky breathes between golden and violet.
    sky_shift = 0.5 + 0.5 * math.sin(phase * 2 * math.pi / 90.0)
    bands = [
        (0.00, PAL["sky_zenith"]),
        (0.25, _lerp(PAL["sky_purple"], PAL["sky_magenta"], sky_shift * 0.4)),
        (0.45, PAL["sky_magenta"]),
        (0.60, PAL["sky_orange"]),
        (0.70, PAL["sky_gold"]),
        (0.72, PAL["sky_horizon"]),
    ]
    for i in range(len(bands) - 1):
        t0, c0 = bands[i]
        t1, c1 = bands[i + 1]
        y0 = int(t0 * HORIZON_Y)
        y1 = int(t1 * HORIZON_Y)
        for y in range(y0, y1):
            tt = (y - y0) / max(1, y1 - y0)
            c = _lerp(c0, c1, tt)
            draw.line([(0, y), (SCENE_W, y)], fill=c)

    # --- Layer 2: sun (slow descent, halo glow) ---
    # Sun travels left→right over 120s, slowly sinking toward horizon.
    sun_cycle = (phase % 120.0) / 120.0
    sun_x = SCENE_W * (0.2 + 0.6 * sun_cycle)
    sun_y = HORIZON_Y * (0.25 + 0.55 * sun_cycle)
    sun_r = 5
    # Halo glow (soft additive blend)
    _pixel_circle_blend(px, sun_x, sun_y, sun_r + 4, PAL["sun_glow"], 0.35,
                        SCENE_W, SCENE_H)
    _pixel_circle_blend(px, sun_x, sun_y, sun_r + 2, PAL["sun_glow"], 0.55,
                        SCENE_W, SCENE_H)
    # Solid disc
    _pixel_circle(px, sun_x, sun_y, sun_r, PAL["sun_core"], SCENE_W, SCENE_H)

    # --- Layer 3: clouds (parallax slow, warm-lit) ---
    # 3 cloud puffs drifting left at ~0.8 px/s. Drawn as clusters of rects.
    cloud_speed = 0.8
    for ci in range(5):
        seed = ci * 17.3
        cx_base = (SCENE_W + 20 - (phase * cloud_speed + seed * 7) % (SCENE_W + 40))
        cy = 8 + int(_hash(ci) * 12)
        cw = 8 + int(_hash(ci + 99) * 6)
        lit = 0.4 + 0.4 * _hash(ci + 50)
        cloud_color = _lerp(PAL["cloud_lo"], PAL["cloud_hi"], lit)
        shadow_color = _lerp(PAL["cloud_lo"], PAL["sky_magenta"], 0.3)
        for bx in range(cw):
            for by in range(3):
                ox = int(cx_base + bx)
                oy = cy + by
                if 0 <= ox < SCENE_W and 0 <= oy < HORIZON_Y:
                    # Cloud body shape: higher in middle, edges taper
                    taper = 1.0 - abs(bx - cw / 2) / (cw / 2) * 0.5
                    if _hash(ci * 100 + bx * 7 + by) < taper:
                        edge = (by == 0 and bx > 1 and bx < cw - 2)
                        px[ox, oy] = cloud_color if edge or by > 0 else shadow_color

    # --- Layer 4: distant mountains (parallax medium-slow) ---
    _triangular_ridge(draw, HORIZON_Y, amplitude=8, period=24,
                      offset=phase * 0.5, color=PAL["mtn_far"], w=SCENE_W)

    # --- Layer 5: floating Laputa fortress (bobbing, glowing windows) ---
    # Centered, slowly bobbing up/down. Blocky silhouette composed of rects.
    bob = math.sin(phase * 2 * math.pi / 5.0) * 1.5
    fx = SCENE_W // 2
    fy = HORIZON_Y - 14 + bob
    # Main body
    fortress_blocks = [
        (-8, 0, 16, 4),   # base platform
        (-6, -3, 12, 3),  # mid wall
        (-4, -6, 8, 3),   # tower base
        (-2, -9, 4, 3),   # spire base
        (-1, -11, 2, 2),  # spire tip
        (-9, 0, 2, 2),    # left buttress
        (7, 0, 2, 2),     # right buttress
    ]
    for bx, by, bw, bh in fortress_blocks:
        draw.rectangle([fx + bx, fy + by, fx + bx + bw - 1, fy + by + bh - 1],
                       fill=PAL["fortress"])
    # Glowing windows (flicker slightly)
    win_flicker = 0.7 + 0.3 * math.sin(phase * 2 * math.pi / 1.3)
    win_color = _lerp(PAL["fortress"], PAL["window"], win_flicker)
    windows = [(-5, -1), (-3, -1), (3, -1), (5, -1), (0, -4), (-1, -7), (1, -7)]
    for wx, wy in windows:
        draw.rectangle([fx + wx, fy + wy, fx + wx, fy + wy], fill=win_color)

    # --- Layer 6: energy shield (pulsing ring around fortress) ---
    # Pulse outward every 2s; intensity falls off with radius.
    shield_phase = (phase % 2.0) / 2.0
    shield_r = 8 + shield_phase * 10
    shield_alpha = (1.0 - shield_phase) * 0.45
    _pixel_circle_blend(px, fx, fy - 4, shield_r, PAL["shield"], shield_alpha,
                        SCENE_W, SCENE_H)

    # --- Layer 7: mid mountains (parallax medium-fast) ---
    _triangular_ridge(draw, HORIZON_Y + 3, amplitude=6, period=18,
                      offset=phase * 1.2, color=PAL["mtn_mid"], w=SCENE_W)

    # --- Layer 8: lightning bolt (periodic jagged strike) ---
    # Strikes every ~4s. Deterministic timing via floor(phase/4) seed.
    strike_period = 4.0
    strike_idx = int(phase / strike_period)
    strike_t = (phase % strike_period) / strike_period  # 0..1 within strike cycle
    flash_duration = 0.18
    if strike_t < flash_duration:
        # Active flash: full-screen brightening + jagged bolt
        bolt_seed = strike_idx
        bolt_x_start = fx + int((_hash(bolt_seed) - 0.5) * 30)
        bolt_x_end = fx + int((_hash(bolt_seed + 7) - 0.5) * 10)
        flash_intensity = (1.0 - strike_t / flash_duration)
        # Jagged bolt: 4 segments from cloud (y=15) to fortress (y=fy-4)
        segs = 5
        prev_x, prev_y = bolt_x_start, 12
        for s in range(1, segs + 1):
            tt = s / segs
            target_y = int(12 + (fy - 4 - 12) * tt)
            jitter = int((_hash(bolt_seed * 13 + s) - 0.5) * 6)
            next_x = int(bolt_x_start + (bolt_x_end - bolt_x_start) * tt) + jitter
            next_x = max(0, min(SCENE_W - 1, next_x))
            draw.line([(prev_x, prev_y), (next_x, target_y)],
                      fill=PAL["lightning"], width=1)
            prev_x, prev_y = next_x, target_y
        # Screen flash: brighten sky near horizon
        flash_color = _lerp(PAL["sky_horizon"], (255, 255, 255), flash_intensity * 0.4)
        for y in range(20, HORIZON_Y):
            for x in range(SCENE_W):
                px[x, y] = _lerp(px[x, y], flash_color, flash_intensity * 0.15)

    # --- Layer 9: forest tree line (nearest, minimal parallax) ---
    _triangular_ridge(draw, HORIZON_Y + 7, amplitude=5, period=12,
                      offset=phase * 2.0, color=PAL["mtn_near"], w=SCENE_W)
    # Individual tree spikes on top of the ridge for forest texture
    for tx in range(0, SCENE_W, 3):
        tree_h = 2 + int(_hash(tx) * 3)
        tree_y_base = HORIZON_Y + 7
        draw.line([(tx, tree_y_base), (tx, tree_y_base - tree_h)],
                  fill=PAL["mtn_near"], width=1)

    # --- Layer 10: rising ember particles ---
    # ~14 deterministic particles. Each rises and resets on a fixed cycle.
    for ei in range(14):
        seed = ei * 23.1
        lifetime = 3.0 + _hash(ei) * 2.0
        cycle_t = (phase + seed) % lifetime / lifetime  # 0=born, 1=reset
        ex = int(_hash(ei + 100) * SCENE_W + math.sin(phase * 2 + ei) * 2) % SCENE_W
        ey = SCENE_H - int(cycle_t * (SCENE_H - HORIZON_Y + 5))
        brightness = (1.0 - cycle_t) * (0.5 + 0.5 * math.sin(phase * 8 + ei))
        if brightness > 0.1 and 0 <= ex < SCENE_W and 0 <= ey < SCENE_H:
            ember_color = _lerp(PAL["ember"], PAL["sun_core"], brightness * 0.5)
            px[ex, ey] = ember_color

    # --- Layer 11: patrolling airship ---
    # Crosses right→left every 18s. Tiny silhouette: hull + fin + propeller.
    ship_cycle = (phase % 18.0) / 18.0
    ship_x = int(SCENE_W - ship_cycle * (SCENE_W + 20))
    ship_y = 22 + int(math.sin(phase * 0.8) * 2)
    if 0 <= ship_x < SCENE_W - 6:
        # Hull (3x1), fin (1x2 above tail), gondola (2x1 below)
        draw.rectangle([ship_x, ship_y, ship_x + 5, ship_y],
                       fill=PAL["airship"])
        draw.rectangle([ship_x + 4, ship_y - 2, ship_x + 4, ship_y - 1],
                       fill=PAL["airship"])
        draw.rectangle([ship_x + 1, ship_y + 1, ship_x + 2, ship_y + 1],
                       fill=PAL["airship"])
        # Propeller blink
        if int(phase * 6) % 2:
            px[ship_x, ship_y] = PAL["mtn_far"]

    return img


def scale_to_canvas(scene_img):
    """Scale a 120x60 scene to the full 480x240 key canvas with NEAREST."""
    return scene_img.resize((CANVAS_W, CANVAS_H), Image.NEAREST)


def slice_tiles(canvas_img, tile_w=120, tile_h=120):
    """Slice the 480x240 canvas into 8 key tiles (4 cols x 2 rows).
    Returns a list of 8 PIL images in key-index order (0-3 = top row, 4-7 = bottom)."""
    tiles = []
    for i in range(8):
        row, col = i // 4, i % 4
        tile = canvas_img.crop((col * tile_w, row * tile_h,
                                (col + 1) * tile_w, (row + 1) * tile_h))
        tiles.append(tile)
    return tiles


def render_touchscreen_banner(phase):
    """Render the 800x100 touchscreen as a Ghibli panorama banner.
    Shows distant mountains + scrolling cloud strip + subtle title text."""
    TW, TH = 800, 100
    img = Image.new("RGB", (TW, TH), PAL["sky_zenith"])
    draw = ImageDraw.Draw(img)

    # Sky gradient (horizontal bands, matching the key scene's golden hour)
    bands = [
        (0.0, PAL["sky_zenith"]),
        (0.3, PAL["sky_purple"]),
        (0.55, PAL["sky_magenta"]),
        (0.75, PAL["sky_orange"]),
        (0.85, PAL["sky_gold"]),
        (1.0, PAL["sky_horizon"]),
    ]
    for i in range(len(bands) - 1):
        t0, c0 = bands[i]
        t1, c1 = bands[i + 1]
        y0 = int(t0 * TH)
        y1 = int(t1 * TH)
        for y in range(y0, y1):
            tt = (y - y0) / max(1, y1 - y0)
            draw.line([(0, y), (TW, y)], fill=_lerp(c0, c1, tt))

    # Sun (mirrors key-scene position)
    sun_cycle = (phase % 120.0) / 120.0
    sx = int(TW * (0.2 + 0.6 * sun_cycle))
    sy = int(TH * 0.4)
    for r, blend in [(22, 0.2), (16, 0.35), (11, 0.6)]:
        px = img.load()
        r2 = r * r
        for y in range(max(0, sy - r), min(TH, sy + r + 1)):
            for x in range(max(0, sx - r), min(TW, sx + r + 1)):
                dx, dy = x - sx, y - sy
                if dx * dx + dy * dy <= r2:
                    px[x, y] = _lerp(px[x, y], PAL["sun_glow"] if r > 12 else PAL["sun_core"], blend)

    # Distant mountain ridge (parallax)
    pts = [(0, TH)]
    x = 0
    while x <= TW:
        ridge_h = int(TH * 0.35 + math.sin(x * 0.03 + phase * 0.3) * 12
                      + math.sin(x * 0.08) * 6)
        pts.append((x, ridge_h))
        x += 8
    pts.append((TW, TH))
    draw.polygon(pts, fill=PAL["mtn_far"])

    # Closer ridge
    pts = [(0, TH)]
    x = 0
    while x <= TW:
        ridge_h = int(TH * 0.55 + math.sin(x * 0.04 + phase * 0.6 + 1.5) * 10
                      + math.sin(x * 0.1) * 5)
        pts.append((x, ridge_h))
        x += 6
    pts.append((TW, TH))
    draw.polygon(pts, fill=PAL["mtn_mid"])

    return img


# ---- self-test -------------------------------------------------------------
if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ghibli_scene_test.png"
    scene = render_scene(0.0)
    canvas = scale_to_canvas(scene)
    tiles = slice_tiles(canvas)
    # Stitch tiles back into a 4x2 grid for a single preview image
    preview = Image.new("RGB", (480, 240), (0, 0, 0))
    for i, tile in enumerate(tiles):
        row, col = i // 4, i % 4
        preview.paste(tile, (col * 120, row * 120))
    preview.save(out)
    print("saved %dx%d preview to %s" % (preview.width, preview.height, out))
    # Also save the touchscreen banner
    banner = render_touchscreen_banner(0.0)
    banner.save(out.replace(".png", "_banner.png"))
    print("saved %dx%d banner to %s" % (banner.width, banner.height,
                                         out.replace(".png", "_banner.png")))
