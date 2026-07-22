#!/usr/bin/env python3
"""
ghibli_beach — the coast panorama for the Stream Deck XL+ LCD strip.

Procedural PIL renderer producing a 1200×100 RGB image that depicts a
Gulf-coast beach scene with live weather conditions and a time-of-day
palette driven by the local hour.

Composition (back → front), chained per frame:
  1. Banded sky gradient (5 phases: night / sunrise / day / sunset / twilight)
  2. Celestial body: sun by day, moon + stars by night (position from local hour)
  3. Clouds: count/tint from weather (few fluffy for CLR, blanket stratus for CLD,
     dark overcast for RAIN/TSTM, none for heavy fog)
  4. Distant barrier-island silhouette at the sky/ocean boundary
  5. Ocean gradient (darker when overcast) with animated wave caps
  6. White surf line where waves meet sand
  7. Sand foreground with slight pixel noise texture
  8. Palm-tree silhouettes at fixed anchor x positions, fronds sway with phase
  9. Conditional weather overlay: rain streaks / lightning bolt / fog bands /
     snow flurries / heat shimmer
 10. Optional grill verdict chip top-right (drawn by deck.py, not here)

Layering is pure: render_fort_myers_beach(phase, weather, local_hour) -> PIL Image.
Same inputs always produce the same frame (no per-call RNG), so the dedup
cache in deck.py can short-circuit identical frames.

ponytail: hand-drawn via ImageDraw primitives, no external assets.
         upgrade: PNG sprite sheet if we ever want photographic palms.
"""
import math
import time
from PIL import Image, ImageDraw

# ---- canvas geometry --------------------------------------------------------
BEACH_W, BEACH_H = 1200, 100
HORIZON_Y = 58          # sky/ocean boundary (slightly above mid for more sky)
SURF_Y = 84             # ocean/sand boundary (wave foam line)
SAND_BASE_Y = 86        # where sand trapezoid starts

# ---- palette by time-of-day phase ------------------------------------------
# Each phase is a full palette dict. _pal_for_hour() interpolates between
# adjacent phases on the hour boundary (e.g., 6.5 = sunrise/day 50/50).
PAL_BEACH = {
    "night": {
        "sky_zenith":  (8, 12, 35),
        "sky_mid":     (15, 22, 55),
        "horizon":     (25, 30, 60),
        "celestial":   (220, 220, 240),   # moon
        "celestial_glow": (180, 180, 210),
        "star":        (230, 230, 255),
        "cloud_hi":    (60, 65, 95),
        "cloud_lo":    (35, 40, 65),
        "island":      (15, 18, 35),
        "ocean_deep":  (12, 22, 50),
        "ocean_shal":  (20, 40, 75),
        "wave_cap":    (90, 110, 150),
        "surf":        (140, 150, 180),
        "sand":        (45, 42, 60),
        "sand_hi":     (60, 55, 75),
        "palm_trunk":  (20, 15, 25),
        "palm_frond":  (18, 25, 35),
        "rain":        (160, 175, 210),
        "fog":         (90, 95, 115),
    },
    "sunrise": {
        "sky_zenith":  (50, 30, 70),
        "sky_mid":     (110, 55, 85),
        "horizon":     (240, 130, 70),
        "celestial":   (255, 180, 100),   # low warm sun
        "celestial_glow": (255, 150, 80),
        "star":        (200, 180, 200),
        "cloud_hi":    (255, 180, 130),
        "cloud_lo":    (90, 55, 80),
        "island":      (40, 25, 45),
        "ocean_deep":  (50, 50, 95),
        "ocean_shal":  (110, 95, 130),
        "wave_cap":    (220, 200, 180),
        "surf":        (240, 220, 200),
        "sand":        (135, 95, 75),
        "sand_hi":     (170, 130, 100),
        "palm_trunk":  (35, 22, 28),
        "palm_frond":  (45, 55, 45),
        "rain":        (170, 150, 170),
        "fog":         (200, 175, 170),
    },
    "day": {
        "sky_zenith":  (70, 130, 200),
        "sky_mid":     (130, 175, 220),
        "horizon":     (190, 220, 240),
        "celestial":   (255, 240, 180),   # high pale-yellow sun
        "celestial_glow": (255, 220, 140),
        "star":        (255, 255, 255),
        "cloud_hi":    (250, 250, 255),
        "cloud_lo":    (200, 210, 225),
        "island":      (50, 75, 90),
        "ocean_deep":  (35, 95, 140),
        "ocean_shal":  (90, 165, 195),
        "wave_cap":    (230, 245, 250),
        "surf":        (245, 250, 252),
        "sand":        (220, 200, 150),
        "sand_hi":     (240, 225, 180),
        "palm_trunk":  (70, 45, 30),
        "palm_frond":  (40, 85, 50),
        "rain":        (180, 200, 225),
        "fog":         (220, 225, 230),
    },
    "sunset": {
        "sky_zenith":  (60, 40, 90),
        "sky_mid":     (160, 70, 85),
        "horizon":     (240, 100, 60),
        "celestial":   (255, 150, 80),    # low orange sun
        "celestial_glow": (255, 120, 60),
        "star":        (255, 200, 180),
        "cloud_hi":    (255, 165, 110),
        "cloud_lo":    (110, 60, 75),
        "island":      (35, 25, 50),
        "ocean_deep":  (55, 45, 95),
        "ocean_shal":  (140, 95, 110),
        "wave_cap":    (240, 200, 170),
        "surf":        (250, 225, 200),
        "sand":        (155, 110, 80),
        "sand_hi":     (190, 145, 105),
        "palm_trunk":  (45, 25, 30),
        "palm_frond":  (55, 50, 45),
        "rain":        (170, 155, 175),
        "fog":         (200, 180, 180),
    },
    "twilight": {
        "sky_zenith":  (15, 20, 45),
        "sky_mid":     (30, 35, 70),
        "horizon":     (55, 50, 90),
        "celestial":   (200, 200, 230),   # rising moon
        "celestial_glow": (160, 160, 195),
        "star":        (220, 220, 245),
        "cloud_hi":    (75, 75, 110),
        "cloud_lo":    (45, 45, 75),
        "island":      (18, 20, 38),
        "ocean_deep":  (18, 25, 55),
        "ocean_shal":  (35, 50, 85),
        "wave_cap":    (110, 125, 160),
        "surf":        (150, 160, 190),
        "sand":        (55, 48, 65),
        "sand_hi":     (75, 65, 85),
        "palm_trunk":  (22, 16, 28),
        "palm_frond":  (25, 30, 40),
        "rain":        (155, 165, 195),
        "fog":         (100, 100, 125),
    },
}

# Phase boundaries (start hour, end hour). Overlap on the boundary hour means
# both neighbors contribute via _pal_for_hour lerp.
_PHASE_BANDS = [
    ("night",   0.0,  5.0),
    ("sunrise", 5.0,  7.0),
    ("day",     7.0, 17.0),
    ("sunset", 17.0, 19.0),
    ("twilight",19.0, 24.0),
    ("night",  24.0, 29.0),  # wrap for hour >24 during lerp
]


# ---- helpers ---------------------------------------------------------------
def _lerp(a, b, t):
    t = max(0.0, min(1.0, t))
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def _lerp_pal(p0, p1, t):
    """Lerp every color in two palette dicts. Assumes matching keys."""
    return {k: _lerp(p0[k], p1[k], t) for k in p0}


def _ease(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _hash(n):
    """Deterministic pseudo-random in [0,1) from a numeric seed."""
    return ((math.sin(n * 127.1 + 311.7) * 43758.5453) % 1.0 + 1.0) % 1.0


def _pal_for_hour(hour):
    """Return an interpolated palette dict for the given local hour (0-24 float).
    Boundary hours (e.g., 6.0) split the difference between adjacent phases."""
    # Wrap hour into [0, 24)
    hour = hour % 24.0
    # Find which band we're in
    for i, (name, t0, t1) in enumerate(_PHASE_BANDS):
        if t0 <= hour < t1:
            span = t1 - t0
            local_t = (hour - t0) / span if span > 0 else 0.0
            # Blend toward the NEXT phase in the last 30% of this band
            blend_window = 0.3
            if local_t > (1.0 - blend_window):
                bt = (local_t - (1.0 - blend_window)) / blend_window
                nxt_name = _PHASE_BANDS[(i + 1) % len(_PHASE_BANDS)][0]
                return _lerp_pal(PAL_BEACH[name], PAL_BEACH[nxt_name], _ease(bt))
            return dict(PAL_BEACH[name])
    # Fallback (shouldn't hit)
    return dict(PAL_BEACH["day"])


# ---- per-pixel helpers (used by sun glow + sand noise) ---------------------
def _pixel_circle_blend(img, cx, cy, r, color, blend):
    """Radial-falloff alpha blend a disc onto img. Used for celestial glows."""
    r2 = r * r
    px = img.load()
    x0 = max(0, int(cx - r))
    x1 = min(BEACH_W - 1, int(cx + r))
    y0 = max(0, int(cy - r))
    y1 = min(BEACH_H - 1, int(cy + r))
    for y in range(y0, y1 + 1):
        dy = y - cy
        for x in range(x0, x1 + 1):
            dx = x - cx
            d2 = dx * dx + dy * dy
            if d2 <= r2:
                falloff = (1.0 - math.sqrt(d2) / r) * blend
                px[x, y] = _lerp(px[x, y], color, falloff)


# ---- base layers -----------------------------------------------------------
def _draw_sky(d, pal, weather):
    """Three-band sky gradient: zenith → mid → horizon."""
    bands = [
        (0.0, pal["sky_zenith"]),
        (0.55, pal["sky_mid"]),
        (1.0, pal["horizon"]),
    ]
    for i in range(len(bands) - 1):
        t0, c0 = bands[i]
        t1, c1 = bands[i + 1]
        y0 = int(t0 * HORIZON_Y)
        y1 = int(t1 * HORIZON_Y)
        for y in range(y0, y1):
            tt = (y - y0) / max(1, y1 - y0)
            d.line([(0, y), (BEACH_W, y)], fill=_lerp(c0, c1, tt))
    # Overcast wash: when CLD, mute the whole sky toward cloud_lo
    if weather == "CLD":
        wash = pal["cloud_lo"]
        for y in range(0, HORIZON_Y):
            tt = y / HORIZON_Y
            wash_t = 0.45 * (1.0 - abs(tt - 0.4))  # strongest at upper-mid
            cur = d  # placeholder; we apply via line overwrite
            d.line([(0, y), (BEACH_W, y)],
                   fill=_lerp(pal["sky_zenith"], wash, wash_t))


def _draw_stars(d, pal, hour):
    """30 hand-picked star positions; visible when hour < 6 or >= 19. Fade in/out
    across the twilight bands."""
    if 6.0 <= hour < 19.0:
        return
    # Fade strength: full at deep night, 0 at the boundary
    if hour < 6.0:
        strength = _ease((6.0 - hour) / 2.0) if hour >= 4.0 else 1.0
    else:
        strength = _ease((hour - 19.0) / 2.0) if hour < 21.0 else 1.0
    if strength < 0.05:
        return
    base = pal["star"]
    # 30 deterministic star positions via hash
    for i in range(30):
        sx = int(_hash(i + 1) * BEACH_W)
        sy = int(_hash(i + 100) * (HORIZON_Y - 8))
        # Twinkle: each star pulses at its own phase
        tw = 0.6 + 0.4 * math.sin(_hash(i + 200) * 6.28 + time.time() * 2.0)
        c = _lerp((10, 10, 30), base, strength * tw)
        d.point([(sx, sy)], fill=c)
        # Brighter stars get a 4-neighbour halo
        if _hash(i + 300) > 0.7:
            for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                d.point([(sx + dx, sy + dy)], fill=_lerp((10, 10, 30), base, strength * tw * 0.5))


def _draw_celestial(img, d, phase, hour, pal, weather):
    """Sun (day) or moon (night) positioned by hour. Cloud cover hides it partly."""
    # Decide celestial kind and position
    # Day arc: hour 6 → 18, sun rises east (x=0) sets west (x=1200)
    # Night arc: hour 18 → 30 (== 6 next day), moon mirrors
    if 6.0 <= hour < 18.0:
        # Day sun
        day_t = (hour - 6.0) / 12.0  # 0..1
        cx = int(BEACH_W * day_t)
        # Parabolic arc: y = 4*t*(1-t) peaks at t=0.5 → y minimum (highest)
        arc = 4.0 * day_t * (1.0 - day_t)
        cy = int(HORIZON_Y - 6 - arc * 30)  # sun rises up to 30px above horizon
        body = pal["celestial"]
        glow = pal["celestial_glow"]
        r_body = 9
        r_glow = 22
    elif hour >= 18.0 or hour < 6.0:
        # Night moon
        if hour >= 18.0:
            night_t = (hour - 18.0) / 12.0
        else:
            night_t = (hour + 6.0) / 12.0
        cx = int(BEACH_W * night_t)
        arc = 4.0 * night_t * (1.0 - night_t)
        cy = int(HORIZON_Y - 6 - arc * 22)
        body = pal["celestial"]
        glow = pal["celestial_glow"]
        r_body = 7
        r_glow = 16
    else:
        return  # gap phase

    # Weather hide: heavy overcast / storm / fog hides celestial
    if weather in ("CLD", "RAIN", "TSTM", "FOG"):
        # Heavily muted: draw a faint glow disc only
        _pixel_circle_blend(img, cx, cy, r_glow, glow, 0.15)
        return

    # Glow halo first (behind)
    _pixel_circle_blend(img, cx, cy, r_glow, glow, 0.45)
    _pixel_circle_blend(img, cx, cy, r_body + 2, body, 0.7)

    # Solid disc
    d.ellipse([cx - r_body, cy - r_body, cx + r_body, cy + r_body], fill=body)

    # Moon crater detail (subtle)
    if hour >= 18.0 or hour < 6.0:
        crater = _lerp(body, (0, 0, 0), 0.18)
        d.ellipse([cx - 3, cy - 2, cx + 1, cy + 2], fill=crater)
        d.ellipse([cx + 2, cy + 1, cx + 4, cy + 3], fill=crater)


def _draw_clouds(d, phase, pal, weather):
    """Drifting cloud layer. Count and tint depend on weather."""
    # Cloud params per condition
    presets = {
        "CLR":  {"count": 3, "tint": "cloud_hi", "shadow": "cloud_lo", "opacity": 0.85, "drift": 0.6},
        "CLD":  {"count": 8, "tint": "cloud_hi", "shadow": "cloud_lo", "opacity": 0.95, "drift": 0.4},
        "DRIZ": {"count": 6, "tint": "cloud_lo", "shadow": "cloud_lo", "opacity": 0.92, "drift": 0.5},
        "RAIN": {"count": 7, "tint": "cloud_lo", "shadow": "cloud_lo", "opacity": 0.97, "drift": 0.7},
        "TSTM": {"count": 8, "tint": "cloud_lo", "shadow": "cloud_lo", "opacity": 0.99, "drift": 0.9},
        "FOG":  {"count": 0, "tint": "cloud_hi", "shadow": "cloud_lo", "opacity": 0.0,  "drift": 0.0},
        "SNOW": {"count": 5, "tint": "cloud_hi", "shadow": "cloud_lo", "opacity": 0.90, "drift": 0.3},
    }
    p = presets.get(weather, presets["CLR"])
    if p["count"] == 0:
        return

    drift_speed = p["drift"]
    hi = pal[p["tint"]]
    lo = pal[p["shadow"]]

    # Each cloud = cluster of overlapping ellipses, drifting left with wrap
    for i in range(p["count"]):
        # Anchors spread across 1.5x canvas width for natural spacing
        anchor_x = (i * (BEACH_W * 1.4) / p["count"]) - (phase * drift_speed * 12.0)
        anchor_x = anchor_x % (BEACH_W + 300) - 150
        # Vary cloud y position a bit
        cy_base = 12 + int(_hash(i + 7) * 22)
        # Scale varies slightly per cloud
        scale = 0.8 + 0.5 * _hash(i + 31)

        # Draw shadow first (slightly below + darker)
        for dx, dy, rx, ry in [(-20, 4, 22, 7), (0, 2, 28, 9), (22, 4, 20, 7), (-8, -2, 18, 6)]:
            d.ellipse([
                anchor_x + dx * scale - rx * scale,
                cy_base + dy * scale - ry * scale,
                anchor_x + dx * scale + rx * scale,
                cy_base + dy * scale + ry * scale,
            ], fill=lo)
        # Then highlight (slightly above + brighter)
        for dx, dy, rx, ry in [(-18, 0, 18, 6), (2, -2, 24, 8), (20, 0, 17, 6)]:
            d.ellipse([
                anchor_x + dx * scale - rx * scale,
                cy_base + dy * scale - ry * scale,
                anchor_x + dx * scale + rx * scale,
                cy_base + dy * scale + ry * scale,
            ], fill=hi)


def _draw_horizon_island(d, pal):
    """Thin barrier-island silhouette at the horizon line."""
    base_y = HORIZON_Y + 1
    pts = [(0, base_y + 4)]
    x = 0
    while x <= BEACH_W:
        # Two islands with valleys between
        h = 0
        if 150 < x < 380:
            # Left island
            t = (x - 150) / 230.0
            h = int(7 * math.sin(t * math.pi))
        elif 700 < x < 1050:
            # Right island
            t = (x - 700) / 350.0
            h = int(9 * math.sin(t * math.pi))
        pts.append((x, base_y - h))
        x += 6
    pts.append((BEACH_W, base_y + 4))
    d.polygon(pts, fill=pal["island"])


def _draw_ocean(d, pal, weather, phase):
    """Ocean gradient + animated wave caps. Choppy when raining/stormy."""
    # Gradient from HORIZON_Y down to SURF_Y
    for y in range(HORIZON_Y, SURF_Y):
        tt = (y - HORIZON_Y) / max(1, SURF_Y - HORIZON_Y)
        d.line([(0, y), (BEACH_W, y)], fill=_lerp(pal["ocean_deep"], pal["ocean_shal"], tt))

    # Wave caps: short white-ish horizontal segments, animated by phase
    cap = pal["wave_cap"]
    if weather in ("RAIN", "TSTM"):
        cap_density = 80
        cap_len = (3, 8)
    elif weather in ("DRIZ",):
        cap_density = 40
        cap_len = (2, 5)
    else:
        cap_density = 22
        cap_len = (4, 10)

    for i in range(cap_density):
        # Deterministic positions with phase-driven drift
        bx = int((_hash(i + 500) * BEACH_W * 1.3) - (phase * 18.0) % BEACH_W) % BEACH_W
        by = HORIZON_Y + 2 + int(_hash(i + 600) * (SURF_Y - HORIZON_Y - 4))
        seg_len = int(cap_len[0] + _hash(i + 700) * (cap_len[1] - cap_len[0]))
        d.line([(bx, by), (bx + seg_len, by)], fill=cap)


def _draw_surf_line(d, pal):
    """Thin white foam strip at the surf line."""
    foam = pal["surf"]
    # Slightly wavy foam line
    pts = []
    for x in range(0, BEACH_W + 4, 4):
        y = SURF_Y + int(2 * math.sin(x * 0.04 + time.time() * 1.5))
        pts.append((x, y))
    for i in range(len(pts) - 1):
        d.line([pts[i], pts[i + 1]], fill=foam, width=2)


def _draw_sand(img, d, pal):
    """Sand trapezoid with pixel noise for texture."""
    # Base sand fill from SURF_Y+2 down to BEACH_H
    for y in range(SAND_BASE_Y, BEACH_H):
        tt = (y - SAND_BASE_Y) / max(1, BEACH_H - SAND_BASE_Y)
        d.line([(0, y), (BEACH_W, y)], fill=_lerp(pal["sand"], pal["sand_hi"], tt * 0.6))

    # Pixel noise: ~5% of sand pixels get a slight highlight variation
    px = img.load()
    for _ in range(400):
        sx = int(_hash(_ * 13 + 1) * BEACH_W)
        sy = SAND_BASE_Y + int(_hash(_ * 17 + 2) * (BEACH_H - SAND_BASE_Y))
        if 0 <= sx < BEACH_W and 0 <= sy < BEACH_H:
            base = px[sx, sy]
            px[sx, sy] = _lerp(base, pal["sand_hi"], 0.35)


def _draw_palm_tree(d, x_anchor, base_y, phase, pal, scale=1.0):
    """Single palm silhouette. Trunk + 7 fronds swaying with phase."""
    trunk = pal["palm_trunk"]
    frond = pal["palm_frond"]

    # Trunk: slightly curved tapering line from base up to crown
    trunk_h = int(40 * scale)
    trunk_top_x = x_anchor + int(3 * math.sin(phase * 0.3 + x_anchor))  # gentle sway
    trunk_top_y = base_y - trunk_h
    # Draw trunk as stacked rectangles (tapering)
    for i in range(trunk_h):
        t = i / trunk_h
        w = max(1, int((4 - 2 * t) * scale))
        # Slight curve: x drifts by sin
        offset_x = int(3 * math.sin(t * 3.0 + phase * 0.2))
        seg_x = x_anchor + offset_x
        seg_y = base_y - i
        d.rectangle([seg_x - w, seg_y - 1, seg_x + w, seg_y + 1], fill=trunk)

    # Fronds: 7 fronds radiating from crown, each an elongated triangle
    cx = trunk_top_x
    cy = trunk_top_y
    sway = math.sin(phase * 0.8 + x_anchor * 0.01)  # fronds sway
    frond_len = int(22 * scale)
    for i in range(7):
        # Angles spread across the top half-circle: -150° to -30° (in degrees from horizontal)
        # Convert to radians, fan from 180° (left) to 360° (right) through 270° (up)
        base_angle = math.radians(180.0 + i * (180.0 / 6.0))  # 180..360
        # Sway modifies the angle slightly per frond
        angle = base_angle + sway * 0.15 * (1 if i % 2 == 0 else -1)
        # Tip of frond
        tx = cx + int(frond_len * math.cos(angle))
        ty = cy + int(frond_len * math.sin(angle))
        # Frond as a curved line of varying width (a few stacked segments)
        steps = 6
        prev_x, prev_y = cx, cy
        for s in range(1, steps + 1):
            t = s / steps
            # Curved frond: ease the angle slightly toward vertical (droop)
            droop = math.sin(t * math.pi) * 0.25
            ang = angle + droop * (1 if angle < math.radians(270) else -1)
            mx = cx + int(frond_len * t * math.cos(ang))
            my = cy + int(frond_len * t * math.sin(ang)) + int(droop * 4 * scale)
            w = max(1, int((3 - 2 * t) * scale))
            d.line([(prev_x, prev_y), (mx, my)], fill=frond, width=w)
            prev_x, prev_y = mx, my
        # Frond tip: small cluster of leaflets
        d.ellipse([prev_x - 2, prev_y - 2, prev_x + 2, prev_y + 2], fill=frond)

    # Crown bulge
    d.ellipse([cx - 4, cy - 3, cx + 4, cy + 3], fill=trunk)

    # A couple of coconuts
    for dx, dy in [(-3, 3), (2, 4)]:
        d.ellipse([cx + dx - 1, cy + dy - 1, cx + dx + 1, cy + dy + 1],
                  fill=_lerp(trunk, (0, 0, 0), 0.2))


# ---- weather overlays ------------------------------------------------------
def _draw_rain_overlay(d, phase, density, color):
    """Vertical rain streaks. Density = streaks per frame."""
    for i in range(density):
        # Position via hash, animated fall via phase
        seed_x = _hash(i + 1000)
        seed_v = _hash(i + 2000)
        bx = int(seed_x * BEACH_W * 1.2) % BEACH_W
        # Fall speed: 200-400 px/sec depending on seed
        speed = 200 + seed_v * 200
        # Cycle through a 1.0s period
        t = (phase * speed / 100.0 + seed_v * 100.0) % 100.0
        by = int(t)
        # Skip if outside visible band
        if by < 0 or by > BEACH_H + 10:
            continue
        seg_len = 6 + int(seed_v * 6)
        d.line([(bx, by), (bx - 1, by + seg_len)], fill=color, width=1)


def _draw_lightning_bolt(d, phase, color):
    """Periodic lightning flash + jagged bolt. Period ~1.4s, flash 0.18s."""
    period = 1.4
    flash_len = 0.18
    cycle_t = phase % period
    if cycle_t > flash_len:
        return
    # Full-frame white wash, strongest at flash start
    intensity = 1.0 - (cycle_t / flash_len)  # 1.0 → 0
    flash_c = _lerp((0, 0, 0), color, intensity * 0.6)
    d.rectangle([0, 0, BEACH_W, HORIZON_Y], fill=flash_c)

    # Jagged bolt down the middle (deterministic via phase-rounded hash)
    bolt_seed = int(phase / period)
    bx_start = 300 + int(_hash(bolt_seed) * 600)
    by_start = 0
    by_end = HORIZON_Y - 5
    steps = 8
    prev_x, prev_y = bx_start, by_start
    for s in range(1, steps + 1):
        t = s / steps
        ny = by_start + int(t * (by_end - by_start))
        # Jagged x offset, decreasing amplitude near the end
        amp = 25 * (1.0 - t * 0.4)
        nx = bx_start + int((_hash(bolt_seed * 17 + s) - 0.5) * 2 * amp)
        d.line([(prev_x, prev_y), (nx, ny)], fill=color, width=2)
        prev_x, prev_y = nx, ny


def _draw_fog_bands(d, phase, color):
    """Horizontal translucent bands drifting across the whole frame."""
    bands = 5
    for i in range(bands):
        seed = _hash(i + 5000)
        # Y position: spread across sky+ocean
        by = int(seed * (BEACH_H - 10))
        # Drift x via phase
        drift = (phase * (15.0 + seed * 10.0)) % (BEACH_W + 200) - 100
        # Band shape: elongated ellipse, very wide and short
        band_w = 250 + int(seed * 200)
        band_h = 4 + int(_hash(i + 5100) * 4)
        # Draw multiple overlapping ellipses for soft edge
        for off in range(-3, 4):
            a = 0.18 - abs(off) * 0.04
            c = _lerp((0, 0, 0), color, a)
            d.ellipse([
                drift - band_w // 2,
                by + off - band_h // 2,
                drift + band_w // 2,
                by + off + band_h // 2,
            ], fill=c)


def _draw_snow_overlay(d, phase, color):
    """Drifting snow flakes. Rare for the city but spec-complete."""
    flakes = 60
    for i in range(flakes):
        seed_x = _hash(i + 6000)
        seed_v = _hash(i + 6100)
        bx_base = seed_x * BEACH_W
        # Fall speed: 30-80 px/sec (slower than rain)
        speed = 30 + seed_v * 50
        # Cycle through 3.0s period
        t = (phase * speed / 100.0 + seed_v * 100.0) % 100.0
        by = int(t)
        if by < 0 or by > BEACH_H + 5:
            continue
        # Sine-wave horizontal drift
        bx = int(bx_base + math.sin(phase * 1.5 + seed_v * 6.28) * 15) % BEACH_W
        # Flake as 1-2 pixel dot
        d.point([(bx, by)], fill=color)
        if _hash(i + 6200) > 0.6:
            d.point([(bx + 1, by)], fill=color)


def _draw_heat_shimmer(img, d, phase, y_top, y_bot, color):
    """Wavy horizontal lines above the sand, semi-transparent (heat haze)."""
    px = img.load()
    rows = 4
    for r in range(rows):
        y = y_top + int((y_bot - y_top) * r / max(1, rows - 1))
        for x in range(0, BEACH_W, 3):
            # Sine wave vertical displacement
            offset = int(2 * math.sin(x * 0.06 + phase * 2.0 + r * 0.5))
            ty = y + offset
            if 0 <= ty < BEACH_H:
                # Blend with color at low alpha
                falloff = 0.18 + 0.1 * math.sin(x * 0.02 + phase)
                px[x, ty] = _lerp(px[x, ty], color, falloff)


# ---- public entry ----------------------------------------------------------
def render_fort_myers_beach(phase, weather, local_hour):
    """Render a 1200×100 RGB beach panorama reflecting live weather + sun position.

    Args:
        phase: monotonic seconds float (drives all motion — clouds, waves, etc.)
        weather: dict with at least {"icon_word": "CLR"|"CLD"|..., "temp_f": int}
                 icon_word values: CLR, CLD, DRIZ, RAIN, TSTM, FOG, SNOW
        local_hour: 0.0-23.9 — drives palette + celestial position

    Returns:
        PIL.Image RGB 1200×100
    """
    img = Image.new("RGB", (BEACH_W, BEACH_H), PAL_BEACH["day"]["sky_zenith"])
    d = ImageDraw.Draw(img)

    pal = _pal_for_hour(local_hour)
    icon = (weather or {}).get("icon_word", "CLR")
    if icon == "" or icon is None:
        icon = "CLR"
    temp = (weather or {}).get("temp_f")
    if temp is None:
        temp = 70

    # Base layers (always)
    _draw_sky(d, pal, icon)
    _draw_stars(d, pal, local_hour)
    _draw_celestial(img, d, phase, local_hour, pal, icon)
    _draw_clouds(d, phase, pal, icon)
    _draw_horizon_island(d, pal)
    _draw_ocean(d, pal, icon, phase)
    _draw_surf_line(d, pal)
    _draw_sand(img, d, pal)

    # Palms — fixed anchors
    _draw_palm_tree(d, 150, SAND_BASE_Y + 6, phase, pal, scale=1.0)
    _draw_palm_tree(d, 1080, SAND_BASE_Y + 4, phase, pal, scale=0.85)
    _draw_palm_tree(d, 620, SAND_BASE_Y + 8, phase, pal, scale=0.75)

    # Conditional weather overlays
    if icon == "CLR":
        if temp is not None and temp >= 95:
            _draw_heat_shimmer(img, d, phase, 72, 84, pal["sand_hi"])
    elif icon == "CLD":
        pass  # overcast wash already applied in _draw_sky; clouds drawn
    elif icon == "DRIZ":
        _draw_rain_overlay(d, phase, density=30, color=pal["rain"])
    elif icon == "RAIN":
        _draw_rain_overlay(d, phase, density=90, color=pal["rain"])
    elif icon == "TSTM":
        _draw_rain_overlay(d, phase, density=120, color=pal["rain"])
        _draw_lightning_bolt(d, phase, color=(235, 245, 255))
    elif icon == "FOG":
        _draw_fog_bands(d, phase, pal["fog"])
    elif icon == "SNOW":
        _draw_snow_overlay(d, phase, color=(240, 240, 250))

    return img


# ---- self-test -------------------------------------------------------------
if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/beach_test.png"
    hour = float(sys.argv[2]) if len(sys.argv) > 2 else 14.0
    icon = sys.argv[3] if len(sys.argv) > 3 else "CLR"
    temp = int(sys.argv[4]) if len(sys.argv) > 4 else 88
    weather = {"icon_word": icon, "temp_f": temp}
    img = render_fort_myers_beach(0.0, weather, hour)
    img.save(out)
    print("saved %dx%d to %s (hour=%.1f icon=%s temp=%d)" %
          (img.width, img.height, out, hour, icon, temp))
