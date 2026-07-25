#!/usr/bin/env python3
"""
ghibli_beach — live-weather beach panorama for the Stream Deck XL+ LCD strip.

Procedural PIL renderer producing a 1200×100 RGB image that depicts a
Gulf-coast beach scene with live weather conditions and a time-of-day
palette driven by the local hour. Styled for Studio Ghibli watercolor feel:
soft radial sun glow, painterly 3-tier clouds, organic curved palm trunks
with leaflet-bearing fronds, banded ocean with swell lines, wispy foam
surf, sand with wet strip + tide lines + warm sparkle grain.

Composition (back → front), chained per frame:
  1. Banded sky gradient (5 phases: night / sunrise / day / sunset / twilight)
  2. Celestial body: sun by day, moon + stars by night — multi-layer soft glow
  3. Clouds: 3-tier painterly puffs (shadow / body / crest) — count + tint from weather
  4. Distant barrier-island silhouette at the sky/ocean boundary
  5. Ocean: 4-band depth gradient + swell lines + animated wave caps
  6. Wispy foam surf line with gaps + secondary backwash
  7. Sand: wet strip near water + horizontal tide lines + warm sparkle grain
  8. Palm-tree silhouettes: curved tapering trunk + 7 fronds each with leaflets
  9. Conditional weather overlay: rain streaks / lightning bolt / fog bands /
     snow flurries / heat shimmer
 10. Optional grill verdict chip top-right (drawn by deck.py, not here)

Layering is pure: render_beach_panorama(phase, weather, local_hour) -> PIL Image.
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
# Day palette warmed slightly toward cream/gold for Ghibli watercolor feel.
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
        "sky_zenith":  (78, 138, 198),    # warmed slightly from (70,130,200)
        "sky_mid":     (140, 182, 218),   # warmer mid
        "horizon":     (205, 228, 240),
        "celestial":   (255, 244, 188),   # warm pale-yellow sun
        "celestial_glow": (255, 224, 148),
        "star":        (255, 255, 255),
        "cloud_hi":    (254, 250, 242),   # cream tint (was 250,250,255)
        "cloud_lo":    (198, 208, 222),
        "island":      (55, 80, 92),
        "ocean_deep":  (32, 88, 132),
        "ocean_shal":  (96, 168, 192),
        "wave_cap":    (232, 246, 250),
        "surf":        (248, 252, 252),
        "sand":        (222, 202, 152),   # warm tan
        "sand_hi":     (244, 228, 182),   # warm cream
        "palm_trunk":  (74, 48, 32),
        "palm_frond":  (52, 102, 56),     # lush green (was 40,85,50)
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
    hour = hour % 24.0
    for i, (name, t0, t1) in enumerate(_PHASE_BANDS):
        if t0 <= hour < t1:
            span = t1 - t0
            local_t = (hour - t0) / span if span > 0 else 0.0
            blend_window = 0.3
            if local_t > (1.0 - blend_window):
                bt = (local_t - (1.0 - blend_window)) / blend_window
                nxt_name = _PHASE_BANDS[(i + 1) % len(_PHASE_BANDS)][0]
                return _lerp_pal(PAL_BEACH[name], PAL_BEACH[nxt_name], _ease(bt))
            return dict(PAL_BEACH[name])
    return dict(PAL_BEACH["day"])


# ---- per-pixel helpers -----------------------------------------------------
def _pixel_circle_blend(img, cx, cy, r, color, blend):
    """Radial-falloff alpha blend a disc onto img. Used for soft celestial glow."""
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


def _band_blend(img, y_center, half_h, color, alpha_peak, x0=0, x1=None):
    """Soft horizontal band with linear falloff in y. Peaks at y_center.
    Used for atmospheric haze, tide bands, wet-sand transitions."""
    if x1 is None:
        x1 = BEACH_W
    if half_h <= 0 or alpha_peak <= 0.0:
        return
    px = img.load()
    y_lo = max(0, int(y_center - half_h))
    y_hi = min(BEACH_H, int(y_center + half_h + 1))
    inv_half = 1.0 / float(half_h)
    for y in range(y_lo, y_hi):
        dist = abs(y - y_center)
        alpha = alpha_peak * max(0.0, 1.0 - dist * inv_half)
        if alpha <= 0.0:
            continue
        for x in range(x0, x1):
            px[x, y] = _lerp(px[x, y], color, alpha)


# ---- base layers -----------------------------------------------------------
def _draw_sky(d, pal, weather):
    """Four-band sky gradient with eased transitions for watercolor feel.
    Overcast wash mutes the whole sky toward cloud_lo when CLD."""
    bands = [
        (0.00, pal["sky_zenith"]),
        (0.35, pal["sky_mid"]),
        (0.75, pal["horizon"]),
        (1.00, _lerp(pal["horizon"], pal["cloud_hi"], 0.20)),  # warm haze at very bottom
    ]
    for i in range(len(bands) - 1):
        t0, c0 = bands[i]
        t1, c1 = bands[i + 1]
        y0 = int(t0 * HORIZON_Y)
        y1 = int(t1 * HORIZON_Y)
        for y in range(y0, y1):
            tt = (y - y0) / max(1, y1 - y0)
            d.line([(0, y), (BEACH_W, y)], fill=_lerp(c0, c1, _ease(tt)))
    # Overcast wash: when CLD, mute the whole sky toward cloud_lo
    if weather == "CLD":
        wash = pal["cloud_lo"]
        for y in range(0, HORIZON_Y):
            tt = y / HORIZON_Y
            wash_t = 0.50 * (1.0 - abs(tt - 0.4))  # strongest at upper-mid
            d.line([(0, y), (BEACH_W, y)],
                   fill=_lerp(pal["sky_zenith"], wash, wash_t))


def _draw_stars(d, pal, hour):
    """30 hand-picked star positions; visible when hour < 6 or >= 19."""
    if 6.0 <= hour < 19.0:
        return
    if hour < 6.0:
        strength = _ease((6.0 - hour) / 2.0) if hour >= 4.0 else 1.0
    else:
        strength = _ease((hour - 19.0) / 2.0) if hour < 21.0 else 1.0
    if strength < 0.05:
        return
    base = pal["star"]
    for i in range(30):
        sx = int(_hash(i + 1) * BEACH_W)
        sy = int(_hash(i + 100) * (HORIZON_Y - 8))
        tw = 0.6 + 0.4 * math.sin(_hash(i + 200) * 6.28 + time.time() * 2.0)
        c = _lerp((10, 10, 30), base, strength * tw)
        d.point([(sx, sy)], fill=c)
        if _hash(i + 300) > 0.7:
            for dx, dy in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                d.point([(sx + dx, sy + dy)], fill=_lerp((10, 10, 30), base, strength * tw * 0.5))


def _draw_celestial(img, d, phase, hour, pal, weather):
    """Sun (day) or moon (night) — soft multi-layer radial glow, no hard disc.
    Ghibli sun = wide warm halo with bright core bloom. Weather hides it partly."""
    if 6.0 <= hour < 18.0:
        day_t = (hour - 6.0) / 12.0
        cx = int(BEACH_W * day_t)
        arc = 4.0 * day_t * (1.0 - day_t)
        cy = int(HORIZON_Y - 6 - arc * 30)
        body = pal["celestial"]
        glow = pal["celestial_glow"]
        r_body = 9
        r_glow = 22
    elif hour >= 18.0 or hour < 6.0:
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
        return

    # Weather hide: heavy overcast / storm / fog — only faint glow disc
    if weather in ("CLD", "RAIN", "TSTM", "FOG"):
        _pixel_circle_blend(img, cx, cy, r_glow, glow, 0.18)
        return

    # Three-tier soft halo (Ghibli watercolor bloom): wide soft → middle → bright core
    _pixel_circle_blend(img, cx, cy, int(r_glow * 1.5), glow, 0.12)   # outermost wide soft
    _pixel_circle_blend(img, cx, cy, r_glow, glow, 0.28)              # middle halo
    _pixel_circle_blend(img, cx, cy, r_body + 4, body, 0.55)          # inner bright corona
    _pixel_circle_blend(img, cx, cy, r_body, body, 0.95)              # core bloom (replaces hard disc)

    # Moon crater detail (subtle)
    if hour >= 18.0 or hour < 6.0:
        crater = _lerp(body, (0, 0, 0), 0.18)
        d.ellipse([cx - 3, cy - 2, cx + 1, cy + 2], fill=crater)
        d.ellipse([cx + 2, cy + 1, cx + 4, cy + 3], fill=crater)


def _draw_clouds(d, phase, pal, weather):
    """Painterly 3-tier clouds (shadow base → body → crest highlight).
    Each cloud = cluster of overlapping ellipses with volume + light direction."""
    presets = {
        "CLR":  {"count": 3, "scale_mu": 1.0, "hi": "cloud_hi", "lo": "cloud_lo", "drift": 0.6},
        "CLD":  {"count": 7, "scale_mu": 1.4, "hi": "cloud_hi", "lo": "cloud_lo", "drift": 0.4},
        "DRIZ": {"count": 5, "scale_mu": 1.2, "hi": "cloud_lo", "lo": "cloud_lo", "drift": 0.5},
        "RAIN": {"count": 7, "scale_mu": 1.5, "hi": "cloud_lo", "lo": "cloud_lo", "drift": 0.7},
        "TSTM": {"count": 8, "scale_mu": 1.6, "hi": "cloud_lo", "lo": "cloud_lo", "drift": 0.9},
        "FOG":  {"count": 0, "scale_mu": 1.0, "hi": "cloud_hi", "lo": "cloud_lo", "drift": 0.0},
        "SNOW": {"count": 5, "scale_mu": 1.2, "hi": "cloud_hi", "lo": "cloud_lo", "drift": 0.3},
    }
    p = presets.get(weather, presets["CLR"])
    if p["count"] == 0:
        return

    hi = pal[p["hi"]]
    lo = pal[p["lo"]]
    body_c = _lerp(lo, hi, 0.55)     # mid-tone between shadow + crest
    drift_speed = p["drift"]

    for i in range(p["count"]):
        anchor_x = (i * (BEACH_W * 1.4) / p["count"]) - (phase * drift_speed * 12.0)
        anchor_x = anchor_x % (BEACH_W + 300) - 150
        cy_base = 12 + int(_hash(i + 7) * 22)
        scale = (p["scale_mu"] - 0.2) + 0.5 * _hash(i + 31)

        # Shadow tier (bottom, widest, darkest) — drawn first
        sh = lo
        for dx, dy, rx, ry in [(-28, 5, 30, 9), (-8, 7, 36, 11), (16, 5, 32, 10), (32, 7, 24, 8)]:
            d.ellipse([
                anchor_x + dx * scale - rx * scale,
                cy_base + dy * scale - ry * scale,
                anchor_x + dx * scale + rx * scale,
                cy_base + dy * scale + ry * scale,
            ], fill=sh)

        # Body tier (middle) — mid-tone, overlapping shadow
        for dx, dy, rx, ry in [(-22, 0, 26, 8), (0, -2, 32, 10), (22, 0, 25, 8)]:
            d.ellipse([
                anchor_x + dx * scale - rx * scale,
                cy_base + dy * scale - ry * scale,
                anchor_x + dx * scale + rx * scale,
                cy_base + dy * scale + ry * scale,
            ], fill=body_c)

        # Crest tier (top, smaller, brightest) — catches the light
        for dx, dy, rx, ry in [(-16, -4, 18, 6), (4, -6, 22, 7), (20, -3, 16, 5)]:
            d.ellipse([
                anchor_x + dx * scale - rx * scale,
                cy_base + dy * scale - ry * scale,
                anchor_x + dx * scale + rx * scale,
                cy_base + dy * scale + ry * scale,
            ], fill=hi)


def _draw_horizon_island(d, pal):
    """Thin barrier-island silhouette at the horizon line. Atmospheric perspective:
    color is faded toward horizon sky for distant feel."""
    base_y = HORIZON_Y + 1
    pts = [(0, base_y + 4)]
    x = 0
    while x <= BEACH_W:
        h = 0
        if 150 < x < 380:
            t = (x - 150) / 230.0
            h = int(7 * math.sin(t * math.pi))
        elif 700 < x < 1050:
            t = (x - 700) / 350.0
            h = int(9 * math.sin(t * math.pi))
        pts.append((x, base_y - h))
        x += 6
    pts.append((BEACH_W, base_y + 4))
    # Fade island color toward horizon for atmospheric depth
    island_faded = _lerp(pal["island"], pal["horizon"], 0.35)
    d.polygon(pts, fill=island_faded)


def _draw_ocean(d, pal, weather, phase):
    """4-band depth ocean gradient + horizontal swell lines + animated wave caps.
    Wave caps biased toward surf (closer = denser). Choppy when raining/stormy."""
    deep = pal["ocean_deep"]
    shal = pal["ocean_shal"]
    # Banded depth gradient: 4 tiers with eased transitions
    band_count = 4
    for y in range(HORIZON_Y, SURF_Y):
        tt = (y - HORIZON_Y) / max(1, SURF_Y - HORIZON_Y)
        band_t = tt * band_count
        band_idx = min(band_count - 1, int(band_t))
        local_t = band_t - band_idx
        t0 = band_idx / band_count
        t1 = (band_idx + 1) / band_count
        c0 = _lerp(deep, shal, t0)
        c1 = _lerp(deep, shal, t1)
        d.line([(0, y), (BEACH_W, y)], fill=_lerp(c0, c1, _ease(local_t)))

    # Long swell lines: 3 subtle horizontal lines across ocean (painterly depth)
    swell_c = _lerp(deep, pal["wave_cap"], 0.18)
    for swell_t in (0.20, 0.50, 0.78):
        sy = HORIZON_Y + int((SURF_Y - HORIZON_Y) * swell_t)
        # Slight phase-driven horizontal shift on each end for living water
        shift = int(2 * math.sin(phase * 0.6 + swell_t * 4))
        d.line([(shift, sy), (BEACH_W, sy)], fill=swell_c, width=1)

    # Wave caps: short bright horizontal segments, biased toward surf
    cap = pal["wave_cap"]
    if weather in ("RAIN", "TSTM"):
        cap_density, cap_len_range = 80, (3, 8)
    elif weather == "DRIZ":
        cap_density, cap_len_range = 40, (2, 5)
    else:
        cap_density, cap_len_range = 30, (4, 12)
    for i in range(cap_density):
        bx = int((_hash(i + 500) * BEACH_W * 1.3) - (phase * 18.0)) % BEACH_W
        # y biased toward surf (lower = closer = more visible)
        y_bias = _hash(i + 600) ** 1.4
        by = HORIZON_Y + 2 + int(y_bias * (SURF_Y - HORIZON_Y - 4))
        seg_len = int(cap_len_range[0] + _hash(i + 700) * (cap_len_range[1] - cap_len_range[0]))
        d.line([(bx, by), (bx + seg_len, by)], fill=cap, width=1)


def _draw_surf_line(d, pal, phase):
    """Wispy irregular foam line with gaps + secondary backwash.
    Ghibli surf isn't a straight line — it's broken foam with wet tendrils."""
    foam = pal["surf"]
    foam_dim = _lerp(foam, _lerp(pal["sand"], (0, 0, 0), 0.10), 0.40)

    # Main foam line with irregular gaps (hash-driven gap placement)
    x = 0
    seg_id = 0
    while x < BEACH_W:
        gap_seed = _hash(seg_id + 11)
        if gap_seed > 0.88:
            # Skip a small gap (wave recession)
            x += int(4 + gap_seed * 8)
            seg_id += 1
            continue
        seg_len = 6 + int(_hash(seg_id + 23) * 12)
        seg_y = SURF_Y + int(2 * math.sin(x * 0.04 + phase * 1.5))
        d.line([(x, seg_y), (min(BEACH_W, x + seg_len), seg_y)], fill=foam, width=2)
        x += seg_len + int(_hash(seg_id + 31) * 4)
        seg_id += 1

    # Secondary thinner foam line above (backwash retracting)
    backwash_y = SURF_Y - 3 + int(1.0 * math.sin(phase * 1.2))
    for x in range(0, BEACH_W, 6):
        gap_seed = _hash(x + 41)
        if gap_seed > 0.55:
            seg_len = 3 + int(_hash(x + 53) * 4)
            d.line([(x, backwash_y), (min(BEACH_W, x + seg_len), backwash_y)],
                   fill=foam_dim, width=1)


def _draw_sand(img, d, pal):
    """Sand: wet-sand strip just below surf + horizontal tide lines +
    warm sparkle grain (denser than before). Painterly Ghibli beach foreground."""
    sand = pal["sand"]
    sand_hi = pal["sand_hi"]
    wet_band_h = 5

    # Wet sand strip (dark, damp)
    wet_c = _lerp(sand, pal["palm_trunk"], 0.35)
    for y in range(SAND_BASE_Y, min(BEACH_H, SAND_BASE_Y + wet_band_h)):
        d.line([(0, y), (BEACH_W, y)], fill=wet_c)

    # Main dry sand gradient
    dry_start = SAND_BASE_Y + wet_band_h
    for y in range(dry_start, BEACH_H):
        tt = (y - dry_start) / max(1, BEACH_H - dry_start)
        d.line([(0, y), (BEACH_W, y)], fill=_lerp(sand, sand_hi, tt * 0.7))

    # Tide lines: 2 subtle darker horizontal bands (waterline history)
    tide_c = _lerp(sand, pal["palm_trunk"], 0.20)
    for tide_off in (wet_band_h + 2, wet_band_h + 6):
        ty = SAND_BASE_Y + tide_off
        if ty < BEACH_H:
            d.line([(0, ty), (BEACH_W, ty)], fill=tide_c, width=1)

    # Warm sparkle grain (denser + variable alpha for painterly texture)
    px = img.load()
    grain_count = 700
    dry_h = BEACH_H - dry_start
    if dry_h <= 0:
        return
    for n in range(grain_count):
        sx = int(_hash(n * 13 + 1) * BEACH_W)
        sy = dry_start + int(_hash(n * 17 + 2) * dry_h)
        if 0 <= sx < BEACH_W and 0 <= sy < BEACH_H:
            base = px[sx, sy]
            # Half bright sparkle, half darker grain — reads as sand texture
            if _hash(n * 19 + 3) > 0.5:
                px[sx, sy] = _lerp(base, sand_hi, 0.40)
            else:
                px[sx, sy] = _lerp(base, _lerp(sand, (0, 0, 0), 0.20), 0.25)


def _draw_palm_tree(d, x_anchor, base_y, phase, pal, scale=1.0):
    """Organic curved-trunk palm with rib-and-leaflet fronds.
    Trunk: curved tapering polygon with highlight + shadow stripes + ring bands.
    Fronds: 7 ribs each with paired leaflets along the length, plus 2 drooping accents."""
    trunk_c = pal["palm_trunk"]
    trunk_hi = _lerp(trunk_c, (255, 240, 200), 0.20)   # warm sunlit side
    trunk_sh = _lerp(trunk_c, (0, 0, 0), 0.40)          # dark shadow side
    frond_c = pal["palm_frond"]
    frond_sh = _lerp(frond_c, (0, 0, 0), 0.30)

    trunk_h = int(38 * scale)
    # Gentle lean bias + phase sway
    sway = math.sin(phase * 0.4 + x_anchor * 0.013)
    crown_x = x_anchor + int(3 * scale * sway + 2 * scale)
    crown_y = base_y - trunk_h

    # --- Trunk as curved polygon with taper ---
    N = 12
    left_edge = []
    right_edge = []
    for i in range(N + 1):
        t = i / N
        y = base_y - int(t * trunk_h)
        # S-curve: gentle bow
        curve = math.sin(t * math.pi * 0.85 + 0.3) * 3.5 * scale
        taper = (1.0 - t * 0.55) * 2.8 * scale
        cx_seg = x_anchor + curve
        left_edge.append((cx_seg - taper, y))
        right_edge.append((cx_seg + taper, y))
    poly = left_edge + list(reversed(right_edge))
    d.polygon(poly, fill=trunk_c)

    # Highlight stripe on sunlit side (1px inside left edge)
    for i in range(N):
        lx0 = int(left_edge[i][0] + 1)
        lx1 = int(left_edge[i + 1][0] + 1)
        y0 = int(left_edge[i][1])
        y1 = int(left_edge[i + 1][1])
        if 0 <= lx0 < BEACH_W and 0 <= lx1 < BEACH_W:
            d.line([(lx0, y0), (lx1, y1)], fill=trunk_hi, width=1)
    # Shadow stripe on right edge
    for i in range(N):
        rx0 = int(right_edge[i][0] - 1)
        rx1 = int(right_edge[i + 1][0] - 1)
        y0 = int(right_edge[i][1])
        y1 = int(right_edge[i + 1][1])
        if 0 <= rx0 < BEACH_W and 0 <= rx1 < BEACH_W:
            d.line([(rx0, y0), (rx1, y1)], fill=trunk_sh, width=1)

    # Trunk ring bands (segmentation every 3 segs)
    for i in range(2, N - 1, 3):
        t = i / N
        y = base_y - int(t * trunk_h)
        curve = math.sin(t * math.pi * 0.85 + 0.3) * 3.5 * scale
        taper = (1.0 - t * 0.55) * 2.8 * scale
        cx_seg = int(x_anchor + curve)
        d.line([(int(cx_seg - taper + 1), y), (int(cx_seg + taper - 1), y)],
               fill=trunk_sh, width=1)

    # Crown bulge (organic ellipse where fronds meet trunk)
    d.ellipse([crown_x - 5, crown_y - 3, crown_x + 5, crown_y + 3], fill=trunk_c)

    # --- Fronds: 7 ribs with paired leaflets + 2 drooping accents ---
    frond_count = 7
    frond_len = int(26 * scale)
    # Fan from 170° (left-horizontal) through 270° (straight up) to 370° (right-horizontal)
    for i in range(frond_count):
        base_angle = math.radians(170.0 + i * (200.0 / (frond_count - 1)))
        per_frond_sway = math.sin(phase * 0.9 + i * 0.7 + x_anchor * 0.01) * 0.18
        angle = base_angle + per_frond_sway

        # Build curved rib: 6 points from crown to tip with progressive droop
        steps = 6
        rib_points = [(crown_x, crown_y)]
        for s in range(1, steps + 1):
            t = s / steps
            droop = math.sin(t * math.pi) * 4 * scale
            mx = crown_x + int(frond_len * t * math.cos(angle))
            my = crown_y + int(frond_len * t * math.sin(angle)) + int(droop)
            rib_points.append((mx, my))

        # Draw rib spine with tapering width
        for s in range(len(rib_points) - 1):
            t = s / max(1, len(rib_points) - 1)
            w = max(1, int((3 - 2 * t) * scale))
            d.line([rib_points[s], rib_points[s + 1]], fill=frond_c, width=w)

        # Leaflets: small angled lines along the rib (alternating sides)
        for s in range(1, len(rib_points) - 1):
            if s % 2 == 0:  # halve leaflet count for perf
                continue
            base_x, base_y_seg = rib_points[s]
            next_x, next_y_seg = rib_points[s + 1]
            prev_x, prev_y_seg = rib_points[s - 1]
            # Rib direction
            rib_dx = next_x - prev_x
            rib_dy = next_y_seg - prev_y_seg
            rib_len = math.hypot(rib_dx, rib_dy) + 0.001
            # Perpendicular unit
            perp_x = -rib_dy / rib_len
            perp_y = rib_dx / rib_len
            t = s / max(1, len(rib_points) - 1)
            leaf_len = int((5 - 3 * t) * scale)
            if leaf_len < 1:
                continue
            # One side (top)
            tip_x = int(base_x + perp_x * leaf_len)
            tip_y = int(base_y_seg + perp_y * leaf_len)
            if 0 <= tip_x < BEACH_W and 0 <= tip_y < BEACH_H:
                d.line([(base_x, base_y_seg), (tip_x, tip_y)], fill=frond_c, width=1)
            # Other side (bottom)
            tip_x2 = int(base_x - perp_x * leaf_len)
            tip_y2 = int(base_y_seg - perp_y * leaf_len)
            if 0 <= tip_x2 < BEACH_W and 0 <= tip_y2 < BEACH_H:
                d.line([(base_x, base_y_seg), (tip_x2, tip_y2)], fill=frond_sh, width=1)

        # Frond tip tuft (small cluster)
        tip_x, tip_y = rib_points[-1]
        d.ellipse([tip_x - 2, tip_y - 2, tip_x + 2, tip_y + 2], fill=frond_c)

    # Drooping accent fronds (2 lower-angle, longer, drooping down)
    for sign, ang_deg in [(-1, 225), (1, 315)]:
        ang = math.radians(ang_deg) + sway * 0.08
        tip_x = crown_x + int(20 * scale * math.cos(ang))
        tip_y = crown_y + int(20 * scale * math.sin(ang)) + int(7 * scale)
        if 0 <= tip_x < BEACH_W:
            d.line([(crown_x, crown_y), (tip_x, tip_y)], fill=frond_c,
                   width=max(1, int(2 * scale)))
            # Small drooping leaflets
            mid_x = (crown_x + tip_x) // 2
            mid_y = (crown_y + tip_y) // 2
            d.line([(mid_x, mid_y), (mid_x + sign * 3, mid_y + 3)], fill=frond_sh, width=1)

    # Coconuts (2-3 small dark spheres)
    for dx, dy in [(-3, 3), (2, 4), (-1, 5)]:
        d.ellipse([crown_x + dx - 1, crown_y + dy - 1, crown_x + dx + 1, crown_y + dy + 1],
                  fill=_lerp(trunk_c, (0, 0, 0), 0.30))


# ---- weather overlays ------------------------------------------------------
def _draw_rain_overlay(d, phase, density, color):
    """Vertical rain streaks. Density = streaks per frame."""
    for i in range(density):
        seed_x = _hash(i + 1000)
        seed_v = _hash(i + 2000)
        bx = int(seed_x * BEACH_W * 1.2) % BEACH_W
        speed = 200 + seed_v * 200
        t = (phase * speed / 100.0 + seed_v * 100.0) % 100.0
        by = int(t)
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
    intensity = 1.0 - (cycle_t / flash_len)
    flash_c = _lerp((0, 0, 0), color, intensity * 0.6)
    d.rectangle([0, 0, BEACH_W, HORIZON_Y], fill=flash_c)
    bolt_seed = int(phase / period)
    bx_start = 300 + int(_hash(bolt_seed) * 600)
    by_start = 0
    by_end = HORIZON_Y - 5
    steps = 8
    prev_x, prev_y = bx_start, by_start
    for s in range(1, steps + 1):
        t = s / steps
        ny = by_start + int(t * (by_end - by_start))
        amp = 25 * (1.0 - t * 0.4)
        nx = bx_start + int((_hash(bolt_seed * 17 + s) - 0.5) * 2 * amp)
        d.line([(prev_x, prev_y), (nx, ny)], fill=color, width=2)
        prev_x, prev_y = nx, ny


def _draw_fog_bands(d, phase, color):
    """Horizontal translucent bands drifting across the whole frame."""
    bands = 5
    for i in range(bands):
        seed = _hash(i + 5000)
        by = int(seed * (BEACH_H - 10))
        drift = (phase * (15.0 + seed * 10.0)) % (BEACH_W + 200) - 100
        band_w = 250 + int(seed * 200)
        band_h = 4 + int(_hash(i + 5100) * 4)
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
    """Drifting snow flakes. Rare for warm climates but spec-complete."""
    flakes = 60
    for i in range(flakes):
        seed_x = _hash(i + 6000)
        seed_v = _hash(i + 6100)
        bx_base = seed_x * BEACH_W
        speed = 30 + seed_v * 50
        t = (phase * speed / 100.0 + seed_v * 100.0) % 100.0
        by = int(t)
        if by < 0 or by > BEACH_H + 5:
            continue
        bx = int(bx_base + math.sin(phase * 1.5 + seed_v * 6.28) * 15) % BEACH_W
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
            offset = int(2 * math.sin(x * 0.06 + phase * 2.0 + r * 0.5))
            ty = y + offset
            if 0 <= ty < BEACH_H:
                falloff = 0.18 + 0.1 * math.sin(x * 0.02 + phase)
                px[x, ty] = _lerp(px[x, ty], color, falloff)


# ---- public entry ----------------------------------------------------------
def render_beach_panorama(phase, weather, local_hour):
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
    _draw_surf_line(d, pal, phase)
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
    img = render_beach_panorama(0.0, weather, hour)
    img.save(out)
    print("saved %dx%d to %s (hour=%.1f icon=%s temp=%d)" %
          (img.width, img.height, out, hour, icon, temp))
