import math
import cairo
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib

import formula

######################################################################################### config

WIN_W, WIN_H = 1024, 768

HEX_SIZE = 48.
HEX_LINE_WIDTH = .2
CURSOR_LINE_WIDTH = 2.5
HANDLE_SIZE_PX = 4.5
FILL_HIT_RADIUS = 12.
HEADER_LEFT_W = 32.
HEADER_TOP_H = 24.
HEADER_FONT_SIZE = 11.

FONT_FACE = "Sans"
FONT_SIZE_FRAC = .5

INITIAL_ANGLE = math.radians(11.7)
ROT_PER_SCROLL = math.radians(5.)
ZOOM_PER_SCROLL = 1.1
MIN_ZOOM = 1. / 3.
MAX_ZOOM = 10. / 3.
HEADER_BORDER_WIDTH = .4

COL_BG = (1., 1., 1.)
COL_FG = (0., 0., 0.)
# excel-ish selection green.
COL_SELECTION = (.1, .5, .3)
COL_FILL_PREVIEW = (1., 0., 0.)
COL_HEADER_BG = (.93, .93, .93)
COL_HEADER_BG_SEL = (.8, .8, .8)
COL_HEADER_FG = (.3, .3, .3)
HEADER_SEL_BORDER_WIDTH = 2.
SEL_LINE_WIDTH = 1.5
SEL_ALPHA = .18

SQRT3 = math.sqrt(3.)

####################################################################################### geometry

def cube_to_pixel(q, r, angle, size):
	# pointy-top axial (q=x_cube, r=z_cube); y=-q-r is redundant.
	# returns offset from grid origin, before translating to screen origin.
	px = size * (SQRT3 * q + SQRT3 / 2. * r)
	py = size * (1.5 * r)
	c, s = math.cos(angle), math.sin(angle)
	return c * px - s * py, s * px + c * py

def pixel_to_axial(sx, sy, view):
	# inverse of cube_to_pixel + grid-origin translation. fractional (q, r).
	dx, dy = sx - view.ox, sy - view.oy
	c, s = math.cos(view.angle), math.sin(view.angle)
	ux = c * dx + s * dy
	uy = -s * dx + c * dy
	r = uy / (view.size * 1.5)
	q = (ux / view.size - SQRT3 / 2. * r) / SQRT3
	return q, r

def axial_round(fq, fr):
	# round fractional (q, r) to nearest hex, preserving x+y+z=0 in cube space.
	fy = -fq - fr
	rx, ry, rz = round(fq), round(fy), round(fr)
	dx, dy, dz = abs(rx - fq), abs(ry - fy), abs(rz - fr)
	if dx > dy and dx > dz: rx = -ry - rz
	elif dy > dz:           ry = -rx - rz
	else:                   rz = -rx - ry
	return rx, ry, rz

def hex_corners(cx, cy, angle, size):
	# pointy-top: untransformed vertices at 30 + k*60 deg, then rotated by angle.
	pts = []
	for k in range(6):
		a = angle + math.pi / 6. + k * math.pi / 3.
		pts.append((cx + size * math.cos(a), cy + size * math.sin(a)))
	return pts

def visible_axial_range(w, h, view):
	# bounding (q, r) box over the 4 screen corners mapped into axial space.
	qs, rs = [], []
	for sx, sy in ((0., 0.), (w, 0.), (0., h), (w, h)):
		q, r = pixel_to_axial(sx, sy, view)
		qs.append(q); rs.append(r)
	return int(min(qs)) - 1, int(max(qs)) + 1, int(min(rs)) - 1, int(max(rs)) + 1

def hex_distance(a, b):
	return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))

def selection_cells(focus, lo, hi):
	# cells (x, y, z) with x+y+z=0 inside the bounding ranges [lo[i], hi[i]].
	# reaches all hex-boundary shapes: single cell, 1D line, 2D parallelogram,
	# radial hex, and general asymmetric hex.
	if focus is None: return
	for x in range(lo[0], hi[0] + 1):
		for y in range(lo[1], hi[1] + 1):
			z = -x - y
			if z < lo[2] or z > hi[2]: continue
			yield (x, y, z)

def is_fillable(view):
	return view.focus is not None

# edge k of a pointy-top hex is between vertices k and k+1. the cell across
# that edge sits at the neighbor direction below. mapping chosen to match the
# vertex-angle convention in hex_corners.
EDGE_NEIGHBORS = [
	( 0, -1,  1),
	(-1,  0,  1),
	(-1,  1,  0),
	( 0,  1, -1),
	( 1,  0, -1),
	( 1, -1,  0),
]
EDGE_ZERO_INDEX = tuple(d.index(0) for d in EDGE_NEIGHBORS)

def fill_direction_and_steps(view, offset_x, offset_y):
	# pick the hex axis most aligned with the drag delta, in screen space.
	# steps = projected pixel distance divided by one hex-step pixel length.
	best_dot = 0.
	best_d = None
	best_vec = None
	for d in NEIGHBOR_DELTAS:
		dx, _, dz = d
		px, py = cube_to_pixel(dx, dz, view.angle, view.size)
		dot = offset_x * px + offset_y * py
		if dot > best_dot:
			best_dot = dot
			best_d = d
			best_vec = (px, py)
	if best_d is None: return None, 0
	mag_sq = best_vec[0] * best_vec[0] + best_vec[1] * best_vec[1]
	steps = int(round(best_dot / mag_sq))
	return best_d, max(0, steps)

def fill_plan(view):
	# compute the new cells for the current fill drag and the string values to
	# write into each one. per-row progression: cells in the original selection
	# are grouped by the coord perpendicular to the fill direction; each row
	# gets its own rule:
	#   - head is a plain numeric literal: arithmetic progression using the step
	#     from (head - d), falling back to step 1.
	#   - head is a formula (starts with '=') or plain text: copy the raw string
	#     into each new cell. relative refs resolve against the new cell.
	#   - head is empty: the cells extend the shape but no value is written.
	d = view.fill_direction
	if d is None or view.fill_steps <= 0: return [], {}
	row_key_idx = d.index(0)
	rows = {}
	for c in selection_cells(view.focus, view.lo, view.hi):
		rows.setdefault(c[row_key_idx], []).append(c)
	new_cells = []
	fills = {}
	for row_cells in rows.values():
		row_cells.sort(key=lambda c: c[0] * d[0] + c[1] * d[1] + c[2] * d[2])
		head = row_cells[-1]
		s = view.cells.get(head)
		head_v = None
		if s is not None and not s.startswith("="):
			try: head_v = float(s)
			except ValueError: pass
		if head_v is not None:
			step = 1.
			prev = (head[0] - d[0], head[1] - d[1], head[2] - d[2])
			ps = view.cells.get(prev)
			if ps is not None and not ps.startswith("="):
				try: step = head_v - float(ps)
				except ValueError: pass
			for k in range(1, view.fill_steps + 1):
				c = (head[0] + k * d[0], head[1] + k * d[1], head[2] + k * d[2])
				new_cells.append(c)
				v = head_v + step * k
				fills[c] = str(int(v)) if v == int(v) else f"{v:g}"
		else:
			for k in range(1, view.fill_steps + 1):
				c = (head[0] + k * d[0], head[1] + k * d[1], head[2] + k * d[2])
				new_cells.append(c)
				if s is not None:
					fills[c] = s
	return new_cells, fills

def selection_handle_point(view):
	# screen pixel of the vertex of any selection cell furthest toward the
	# bottom-right (max of screen_x + screen_y).
	if view.focus is None: return None
	best, best_key = None, None
	size = view.size
	for (x, _y, z) in selection_cells(view.focus, view.lo, view.hi):
		px, py = cube_to_pixel(x, z, view.angle, size)
		cx, cy = view.ox + px, view.oy + py
		for k in range(6):
			a = view.angle + math.pi / 6. + k * math.pi / 3.
			vx = cx + size * math.cos(a)
			vy = cy + size * math.sin(a)
			key = vx + vy
			if best_key is None or key > best_key:
				best, best_key = (vx, vy), key
	return best

###################################################################################### rendering

def hex_path(ctx, cx, cy, angle, size):
	pts = hex_corners(cx, cy, angle, size)
	ctx.move_to(*pts[0])
	for p in pts[1:]:
		ctx.line_to(*p)
	ctx.close_path()

def draw_region_outline(ctx, view, cells, color):
	# edge-by-edge union outline: for each cell, draw only edges whose neighbor
	# across is not also in the set.
	if not cells: return
	ctx.set_source_rgb(*color)
	ctx.set_line_width(SEL_LINE_WIDTH)
	for c in cells:
		px, py = cube_to_pixel(c[0], c[2], view.angle, view.size)
		cx, cy = view.ox + px, view.oy + py
		corners = hex_corners(cx, cy, view.angle, view.size)
		for k in range(6):
			dn = EDGE_NEIGHBORS[k]
			nb = (c[0] + dn[0], c[1] + dn[1], c[2] + dn[2])
			if nb in cells: continue
			ctx.move_to(*corners[k])
			ctx.line_to(*corners[(k + 1) % 6])
			ctx.stroke()

def draw_selection_fill(ctx, w, h, view):
	if view.focus is None: return
	ctx.set_source_rgba(*COL_SELECTION, SEL_ALPHA)
	size, ox, oy = view.size, view.ox, view.oy
	for cell in selection_cells(view.focus, view.lo, view.hi):
		# while editing, leave the focus cell un-tinted so the caret is clear.
		if view.editing and cell == view.focus: continue
		x, _y, z = cell
		px, py = cube_to_pixel(x, z, view.angle, size)
		cx, cy = ox + px, oy + py
		if cx < -size or cx > w + size: continue
		if cy < -size or cy > h + size: continue
		hex_path(ctx, cx, cy, view.angle, size)
		ctx.fill()

def draw_selection_outline(ctx, w, h, view):
	if view.focus is None: return
	cells = set(selection_cells(view.focus, view.lo, view.hi))
	draw_region_outline(ctx, view, cells, COL_SELECTION)

def draw_grid_lines(ctx, w, h, view):
	ctx.set_source_rgb(*COL_FG)
	ctx.set_line_width(HEX_LINE_WIDTH)
	angle, size, ox, oy = view.angle, view.size, view.ox, view.oy
	q0, q1, r0, r1 = visible_axial_range(w, h, view)
	for q in range(q0, q1 + 1):
		for r in range(r0, r1 + 1):
			px, py = cube_to_pixel(q, r, angle, size)
			cx, cy = ox + px, oy + py
			if cx < -size or cx > w + size: continue
			if cy < -size or cy > h + size: continue
			hex_path(ctx, cx, cy, angle, size)
			ctx.stroke()

def display_text(raw, cell, cells, cache):
	# a formula cell shows its evaluated value; everything else shows raw.
	# evaluation failures render as "#ERR"; cycles render as "#CYCLE".
	if not raw.startswith("="): return raw
	try:
		v = formula.parse_and_eval(raw[1:], cell, cells, cache=cache)
	except formula.EvalError as e:
		return "#CYCLE" if "cycle" in str(e) else "#ERR"
	except Exception:
		return "#ERR"
	return f"{v:g}"

def cell_numeric_value(raw, cell, cells, cache):
	# formula -> evaluated, plain text -> float, else None.
	if raw.startswith("="):
		try: return formula.parse_and_eval(raw[1:], cell, cells, cache=cache)
		except Exception: return None
	try: return float(raw)
	except ValueError: return None

def draw_cell_contents(ctx, w, h, view):
	ctx.set_source_rgb(*COL_FG)
	ctx.select_font_face(FONT_FACE)
	ctx.set_font_size(view.size * FONT_SIZE_FRAC)
	size = view.size
	for cell, raw in view.cells.items():
		if not raw: continue
		# in edit mode on the focus cell, show the raw formula so the caret
		# indexes into what the user is typing; everywhere else show the
		# evaluated display.
		if cell == view.focus and view.editing: s = raw
		else:                                   s = display_text(raw, cell, view.cells, view.eval_cache)
		if not s: continue
		x, _y, z = cell
		px, py = cube_to_pixel(x, z, view.angle, size)
		cx, cy = view.ox + px, view.oy + py
		if cx < -size or cx > w + size: continue
		if cy < -size or cy > h + size: continue
		xb, yb, tw, th, _, _ = ctx.text_extents(s)
		ctx.move_to(cx - tw / 2. - xb, cy - th / 2. - yb)
		ctx.show_text(s)

def draw_cursor(ctx, view):
	x, _y, z = view.focus
	px, py = cube_to_pixel(x, z, view.angle, view.size)
	cx, cy = view.ox + px, view.oy + py
	ctx.set_source_rgb(*COL_SELECTION)
	ctx.set_line_width(CURSOR_LINE_WIDTH)
	hex_path(ctx, cx, cy, view.angle, view.size)
	ctx.stroke()

def draw_caret(ctx, view):
	if not view.editing: return
	ctx.select_font_face(FONT_FACE)
	ctx.set_font_size(view.size * FONT_SIZE_FRAC)
	x, _y, z = view.focus
	px, py = cube_to_pixel(x, z, view.angle, view.size)
	cx, cy = view.ox + px, view.oy + py
	s = view.cells.get(view.focus, "")
	_, _, font_h, _, _ = ctx.font_extents()
	if s:
		xb, _, tw, _, _, _ = ctx.text_extents(s)
		start_x = cx - tw / 2. - xb
		before = s[:view.caret_pos]
		if before:
			_, _, _, _, xa_before, _ = ctx.text_extents(before)
			caret_x = start_x + xa_before
		else:
			caret_x = start_x
	else:
		caret_x = cx
	ctx.set_source_rgb(*COL_SELECTION)
	ctx.set_line_width(1.)
	ctx.move_to(caret_x, cy - font_h / 2.)
	ctx.line_to(caret_x, cy + font_h / 2.)
	ctx.stroke()

def draw_handle(ctx, view):
	if not is_fillable(view): return
	pt = selection_handle_point(view)
	if pt is None: return
	hx, hy = pt
	ctx.set_source_rgb(*COL_SELECTION)
	hex_path(ctx, hx, hy, view.angle, HANDLE_SIZE_PX)
	ctx.fill()

def draw_fill_preview(ctx, view):
	if not view.filling: return
	new_cells, _ = fill_plan(view)
	draw_region_outline(ctx, view, set(new_cells), COL_FILL_PREVIEW)

def _cell_with_coord(idx, v):
	# any valid cube cell with coord idx set to v.
	if idx == 0: return (v, 0, -v)
	if idx == 1: return (0, v, -v)
	return (-v, 0, v)

def _coord_range(w, h, view, idx):
	q0, q1, r0, r1 = visible_axial_range(w, h, view)
	if idx == 0: return range(q0, q1 + 1)
	if idx == 2: return range(r0, r1 + 1)
	return range(-q1 - r1, -q0 - r0 + 1)

# for each fuse_axis a, the two consecutive right-side edges (those whose
# neighbor sits at fuse_axis = +1) start at this k. the three right-side
# vertices in order along the canonical fuse direction are corners[k_start..+2].
# the canonical direction is chosen so iterating cells in it and emitting
# vertices k_start..k_start+2 per cell produces a connected zigzag polyline.
_K_START_RIGHT   = (4, 2, 0)
_CANONICAL_FUSE  = (( 0, -1,  1), ( 1,  0, -1), (-1,  1,  0))

def zigzag_vertices(view, fuse_axis, v, bbox):
	# ordered vertices of the zigzag between cells of fuse_axis=v and v+1.
	# only iterates cells whose center sits inside bbox padded by one hex size.
	k_start = _K_START_RIGHT[fuse_axis]
	direction = _CANONICAL_FUSE[fuse_axis]
	size, angle = view.size, view.angle
	ref = _cell_with_coord(fuse_axis, v)
	rx, ry = cube_to_pixel(ref[0], ref[2], angle, size)
	cx0 = view.ox + rx
	cy0 = view.oy + ry
	sx, sy = cube_to_pixel(direction[0], direction[2], angle, size)
	xmin, ymin, xmax, ymax = bbox
	pad = size
	# find n range such that (cx0 + n*sx, cy0 + n*sy) in [xmin-pad, xmax+pad] x [ymin-pad, ymax+pad].
	n_lo, n_hi = -1e9, 1e9
	for s, c0, lo, hi in ((sx, cx0, xmin - pad, xmax + pad),
			(sy, cy0, ymin - pad, ymax + pad)):
		if s == 0.:
			if not (lo <= c0 <= hi): return []
			continue
		a = (lo - c0) / s
		b = (hi - c0) / s
		if a > b: a, b = b, a
		n_lo = max(n_lo, math.ceil(a))
		n_hi = min(n_hi, math.floor(b))
	if n_lo > n_hi: return []
	# precompute the three vertex offsets from any cell center.
	offs = []
	for i in range(3):
		a = angle + math.pi / 6. + ((k_start + i) % 6) * math.pi / 3.
		offs.append((size * math.cos(a), size * math.sin(a)))
	pts = []
	for n in range(int(n_lo), int(n_hi) + 1):
		cx = cx0 + n * sx
		cy = cy0 + n * sy
		for ox, oy in offs:
			pts.append((cx + ox, cy + oy))
	return pts

def _interp_x_at_y(pts, y):
	for i in range(len(pts) - 1):
		x1, y1 = pts[i]
		x2, y2 = pts[i + 1]
		if y1 == y2: continue
		if min(y1, y2) <= y <= max(y1, y2):
			return x1 + (y - y1) * (x2 - x1) / (y2 - y1)
	return None

def _interp_y_at_x(pts, x):
	for i in range(len(pts) - 1):
		x1, y1 = pts[i]
		x2, y2 = pts[i + 1]
		if x1 == x2: continue
		if min(x1, x2) <= x <= max(x1, x2):
			return y1 + (x - x1) * (y2 - y1) / (x2 - x1)
	return None

def _strip_v_range(view, bbox, fuse_axis):
	# fuse_axis coord values of cells reaching into the bbox. derived from the
	# cube coord at the four bbox corners.
	xmin, ymin, xmax, ymax = bbox
	vs = []
	for sx, sy in ((xmin, ymin), (xmax, ymin), (xmin, ymax), (xmax, ymax)):
		q, r = pixel_to_axial(sx, sy, view)
		cube = (q, -q - r, r)
		vs.append(cube[fuse_axis])
	return int(math.floor(min(vs))) - 1, int(math.ceil(max(vs))) + 1

def _draw_header_grid_strip(ctx, w, h, view, clip_rect, fuse_axis):
	ctx.save()
	ctx.rectangle(*clip_rect); ctx.clip()
	ctx.set_source_rgb(*COL_FG)
	ctx.set_line_width(HEADER_BORDER_WIDTH)
	bbox = (clip_rect[0], clip_rect[1],
			clip_rect[0] + clip_rect[2], clip_rect[1] + clip_rect[3])
	v_lo, v_hi = _strip_v_range(view, bbox, fuse_axis)
	for v in range(v_lo, v_hi + 1):
		pts = zigzag_vertices(view, fuse_axis, v, bbox)
		if not pts: continue
		ctx.move_to(*pts[0])
		for pt in pts[1:]:
			ctx.line_to(*pt)
	ctx.stroke()
	ctx.restore()

def _label_pos_top(v, iv, view, down_px, down_py):
	cell = _cell_with_coord(iv, v)
	px, py = cube_to_pixel(cell[0], cell[2], view.angle, view.size)
	cx, cy = view.ox + px, view.oy + py
	t = (HEADER_TOP_H / 2. - cy) / down_py
	return cx + t * down_px

def _label_pos_left(v, ih, view, right_px, right_py):
	cell = _cell_with_coord(ih, v)
	px, py = cube_to_pixel(cell[0], cell[2], view.angle, view.size)
	cx, cy = view.ox + px, view.oy + py
	t = (HEADER_LEFT_W / 2. - cx) / right_px
	return cy + t * right_py

def _draw_sel_bg_top(ctx, w, h, view, iv, lo_v, hi_v):
	# extend bbox above and below the strip so the polygon's implicit top and
	# bottom closures sit outside the clip rectangle.
	bbox = (HEADER_LEFT_W, -2. * view.size, w, HEADER_TOP_H + 2. * view.size)
	left = zigzag_vertices(view, iv, lo_v - 1, bbox)
	right = zigzag_vertices(view, iv, hi_v, bbox)
	if not left or not right: return
	ctx.save()
	ctx.rectangle(HEADER_LEFT_W, 0, w - HEADER_LEFT_W, HEADER_TOP_H); ctx.clip()
	ctx.set_source_rgb(*COL_HEADER_BG_SEL)
	ctx.move_to(*left[0])
	for pt in left[1:]: ctx.line_to(*pt)
	for pt in reversed(right): ctx.line_to(*pt)
	ctx.close_path(); ctx.fill()
	ctx.restore()

def _draw_sel_bg_left(ctx, w, h, view, ih, lo_v, hi_v):
	bbox = (-2. * view.size, HEADER_TOP_H, HEADER_LEFT_W + 2. * view.size, h)
	top = zigzag_vertices(view, ih, lo_v - 1, bbox)
	bot = zigzag_vertices(view, ih, hi_v, bbox)
	if not top or not bot: return
	ctx.save()
	ctx.rectangle(0, HEADER_TOP_H, HEADER_LEFT_W, h - HEADER_TOP_H); ctx.clip()
	ctx.set_source_rgb(*COL_HEADER_BG_SEL)
	ctx.move_to(*top[0])
	for pt in top[1:]: ctx.line_to(*pt)
	for pt in reversed(bot): ctx.line_to(*pt)
	ctx.close_path(); ctx.fill()
	ctx.restore()

def _draw_sel_border_top(ctx, w, h, view, iv, lo_v, hi_v):
	y = HEADER_TOP_H - 1.
	bbox = (HEADER_LEFT_W, y - view.size, w, y + view.size)
	left = zigzag_vertices(view, iv, lo_v - 1, bbox)
	right = zigzag_vertices(view, iv, hi_v, bbox)
	lx = _interp_x_at_y(left, y)
	rx = _interp_x_at_y(right, y)
	if lx is None or rx is None: return
	if lx > rx: lx, rx = rx, lx
	lx = max(lx, HEADER_LEFT_W); rx = min(rx, w)
	if lx >= rx: return
	ctx.set_source_rgb(*COL_SELECTION)
	ctx.set_line_width(HEADER_SEL_BORDER_WIDTH)
	ctx.move_to(lx, y); ctx.line_to(rx, y); ctx.stroke()

def _draw_sel_border_left(ctx, w, h, view, ih, lo_v, hi_v):
	x = HEADER_LEFT_W - 1.
	bbox = (x - view.size, HEADER_TOP_H, x + view.size, h)
	top = zigzag_vertices(view, ih, lo_v - 1, bbox)
	bot = zigzag_vertices(view, ih, hi_v, bbox)
	ty = _interp_y_at_x(top, x)
	by = _interp_y_at_x(bot, x)
	if ty is None or by is None: return
	if ty > by: ty, by = by, ty
	ty = max(ty, HEADER_TOP_H); by = min(by, h)
	if ty >= by: return
	ctx.set_source_rgb(*COL_SELECTION)
	ctx.set_line_width(HEADER_SEL_BORDER_WIDTH)
	ctx.move_to(x, ty); ctx.line_to(x, by); ctx.stroke()

def draw_headers(ctx, w, h, view):
	down  = neighbor_for_screen_direction(view, math.pi / 2.)
	right = neighbor_for_screen_direction(view, 0.)
	iv = down.index(0)
	ih = right.index(0)
	down_px, down_py = cube_to_pixel(down[0], down[2], view.angle, view.size)
	right_px, right_py = cube_to_pixel(right[0], right[2], view.angle, view.size)
	top_ok  = down_py != 0.
	left_ok = right_px != 0.
	sel = view.focus is not None
	# strip backgrounds. the corner stays untouched (sheet background shows).
	ctx.set_source_rgb(*COL_HEADER_BG)
	ctx.rectangle(HEADER_LEFT_W, 0, w - HEADER_LEFT_W, HEADER_TOP_H); ctx.fill()
	ctx.rectangle(0, HEADER_TOP_H, HEADER_LEFT_W, h - HEADER_TOP_H); ctx.fill()
	# selection-range darker bg: parallelogram bounded by the two zigzags
	# flanking the selection and by the strip's inner top/bottom edges.
	if sel:
		_draw_sel_bg_top (ctx, w, h, view, iv, view.lo[iv], view.hi[iv])
		_draw_sel_bg_left(ctx, w, h, view, ih, view.lo[ih], view.hi[ih])
	# fused hex grid in each strip (one connected polyline per zigzag).
	_draw_header_grid_strip(ctx, w, h, view,
			(HEADER_LEFT_W, 0, w - HEADER_LEFT_W, HEADER_TOP_H), iv)
	_draw_header_grid_strip(ctx, w, h, view,
			(0, HEADER_TOP_H, HEADER_LEFT_W, h - HEADER_TOP_H), ih)
	# flat-ui divider lines between the strips and the sheet. .5 offset puts
	# the stroke center on a pixel-row/column center. the dividers stop at the
	# corner edges so nothing is drawn in the corner.
	ctx.set_source_rgb(*COL_FG)
	ctx.set_line_width(HEADER_BORDER_WIDTH)
	y = HEADER_TOP_H - .5
	ctx.move_to(HEADER_LEFT_W, y); ctx.line_to(w, y); ctx.stroke()
	x = HEADER_LEFT_W - .5
	ctx.move_to(x, HEADER_TOP_H); ctx.line_to(x, h); ctx.stroke()
	# thickened green border piece spanning the selection, snapped to the
	# zigzag intersections at the inner edge of the strip.
	if sel:
		_draw_sel_border_top (ctx, w, h, view, iv, view.lo[iv], view.hi[iv])
		_draw_sel_border_left(ctx, w, h, view, ih, view.lo[ih], view.hi[ih])
	# labels: coord number per column/row. green when inside the selection
	# range; default header-fg otherwise.
	ctx.select_font_face(FONT_FACE)
	ctx.set_font_size(HEADER_FONT_SIZE)
	mid_y = HEADER_TOP_H / 2.
	if top_ok:
		for v in _coord_range(w, h, view, iv):
			lx = _label_pos_top(v, iv, view, down_px, down_py)
			if lx < HEADER_LEFT_W or lx > w: continue
			s = str(v)
			xb, yb, tw, th, _, _ = ctx.text_extents(s)
			if sel and view.lo[iv] <= v <= view.hi[iv]:
				ctx.set_source_rgb(*COL_SELECTION)
			else:
				ctx.set_source_rgb(*COL_HEADER_FG)
			ctx.move_to(lx - tw / 2. - xb, mid_y - th / 2. - yb)
			ctx.show_text(s)
	mid_x = HEADER_LEFT_W / 2.
	if left_ok:
		for v in _coord_range(w, h, view, ih):
			ly = _label_pos_left(v, ih, view, right_px, right_py)
			if ly < HEADER_TOP_H or ly > h: continue
			s = str(v)
			xb, yb, tw, th, _, _ = ctx.text_extents(s)
			if sel and view.lo[ih] <= v <= view.hi[ih]:
				ctx.set_source_rgb(*COL_SELECTION)
			else:
				ctx.set_source_rgb(*COL_HEADER_FG)
			ctx.move_to(mid_x - tw / 2. - xb, ly - th / 2. - yb)
			ctx.show_text(s)

def draw(ctx, w, h, view):
	ctx.set_antialias(cairo.ANTIALIAS_BEST)
	ctx.set_source_rgb(*COL_BG)
	ctx.paint()
	ctx.save()
	ctx.rectangle(HEADER_LEFT_W, HEADER_TOP_H, w - HEADER_LEFT_W, h - HEADER_TOP_H)
	ctx.clip()
	draw_selection_fill(ctx, w, h, view)
	draw_grid_lines(ctx, w, h, view)
	draw_cell_contents(ctx, w, h, view)
	draw_selection_outline(ctx, w, h, view)
	if view.focus is not None:
		draw_cursor(ctx, view)
		draw_handle(ctx, view)
		draw_caret(ctx, view)
	draw_fill_preview(ctx, view)
	ctx.restore()
	draw_headers(ctx, w, h, view)

########################################################################################## state

class View:
	def __init__(self):
		self.angle = INITIAL_ANGLE
		self.size = HEX_SIZE
		self.ox = WIN_W / 2.
		self.oy = WIN_H / 2.
		# drag anchor for mousewheel-button pan.
		self.pan_ox0 = 0.
		self.pan_oy0 = 0.
		# cell model.
		# NOTE: never write to self.cells directly. go through set_cell(view,
		# cell, raw) so the dep graph (self.deps, self.dependents) stays in
		# sync and self.eval_cache invalidates for the touched cell plus its
		# transitive dependents. a direct write skips invalidation and leaves
		# downstream formulas showing stale values.
		self.cells = {}         # dict[(x, y, z) -> str]
		# selection; `focus` is the typing cursor and the cell under the thick
		# outline. (lo, hi) are the bounding ranges of the shape in cube coords.
		# single cell: lo == hi == focus. shift_sel marks whether the current
		# button-1 drag is a shift drag (symmetric radial growth around focus).
		self.focus = None
		self.lo = None
		self.hi = None
		self.shift_sel = False
		# screen anchor for the selection drag.
		self.sel_sx0 = 0.
		self.sel_sy0 = 0.
		# in-memory clipboard. keyed by offset from the focus cell at copy time.
		self.clipboard = {}  # dict[(dx, dy, dz) -> str]
		# fill drag state. during a drag starting on the handle, the current
		# selection stays frozen; fill_direction and fill_steps describe the
		# previewed extension line out from the selection's edge.
		self.filling = False
		self.fill_direction = None
		self.fill_steps = 0
		# custom cursor shown while the pointer is over the fill handle.
		self.tristar_cursor = None
		# True while the focus cell is being typed into. a focus move resets it,
		# so the next printable keystroke replaces the cell content.
		self.editing = False
		# caret index into the focus cell's text, in [0, len]. only meaningful
		# while editing; reset whenever edit mode is entered.
		self.caret_pos = 0
		# cell content captured when edit mode was last entered; Escape restores it.
		self.pre_edit = ""
		# status bar labels, assigned in on_activate.
		self.left_label = None
		self.right_label = None
		# formula bar entry (Gtk.Entry), assigned in on_activate.
		self.entry = None
		# flag set while refresh_entry is pushing cell content into the entry,
		# so the entry's "changed" callback doesn't ricochet back.
		self._updating_entry = False
		# memo of resolved cell values. invalidated per-cell by set_cell, which
		# walks the dependents graph to invalidate transitive readers.
		self.eval_cache = {}
		# dep graph. deps[c] = set of cells c reads. dependents[c] = set of
		# cells that read c. set_cell maintains both.
		self.deps = {}
		self.dependents = {}

class Mouse:
	def __init__(self):
		self.x = WIN_W / 2.
		self.y = WIN_H / 2.

def rotate_around(view, sx, sy, delta):
	# rotate view by delta while keeping screen point (sx, sy) over the same grid cell.
	vx, vy = sx - view.ox, sy - view.oy
	c, s = math.cos(delta), math.sin(delta)
	view.ox = sx - (c * vx - s * vy)
	view.oy = sy - (s * vx + c * vy)
	view.angle += delta

def zoom_around(view, sx, sy, factor):
	# scale size by factor while keeping screen point (sx, sy) over the same grid cell.
	new_size = max(HEX_SIZE * MIN_ZOOM, min(HEX_SIZE * MAX_ZOOM, view.size * factor))
	factor = new_size / view.size
	view.ox = sx - factor * (sx - view.ox)
	view.oy = sy - factor * (sy - view.oy)
	view.size = new_size

def cell_at_pixel(sx, sy, view):
	fq, fr = pixel_to_axial(sx, sy, view)
	return axial_round(fq, fr)

# the 6 cube unit directions to the hex neighbors.
NEIGHBOR_DELTAS = (
	( 1, -1,  0), (-1,  1,  0),
	( 0,  1, -1), ( 0, -1,  1),
	( 1,  0, -1), (-1,  0,  1),
)

def angle_diff(a, b):
	# absolute angular distance in [0, pi].
	d = (a - b) % (2. * math.pi)
	return d if d <= math.pi else 2. * math.pi - d

def neighbor_for_screen_direction(view, screen_angle):
	# cube delta whose on-screen direction is closest to screen_angle.
	best, best_err = None, None
	for d in NEIGHBOR_DELTAS:
		dx, _dy, dz = d
		px, py = cube_to_pixel(dx, dz, view.angle, 1.)
		a = math.atan2(py, px)
		err = angle_diff(a, screen_angle)
		if best_err is None or err < best_err:
			best, best_err = d, err
	return best

def move_cursor(view, delta, extend, area):
	fx, fy, fz = view.focus
	new_focus = (fx + delta[0], fy + delta[1], fz + delta[2])
	if extend:
		# extend the shape along delta: push hi where delta is positive and lo
		# where delta is negative. unchanged axes stay fixed.
		lo = list(view.lo)
		hi = list(view.hi)
		for i in range(3):
			if delta[i] > 0: hi[i] += delta[i]
			elif delta[i] < 0: lo[i] += delta[i]
		view.lo = tuple(lo)
		view.hi = tuple(hi)
	else:
		view.lo = new_focus
		view.hi = new_focus
	view.focus = new_focus
	view.editing = False
	ensure_focus_visible(view, area)

def ensure_focus_visible(view, area):
	# nudge (ox, oy) so the focus cell sits inside the viewport with a one-hex
	# margin. called after cursor moves, not after rotate/zoom/pan.
	if view.focus is None: return
	w = area.get_width()
	h = area.get_height()
	if w <= 0 or h <= 0: return
	x, _y, z = view.focus
	px, py = cube_to_pixel(x, z, view.angle, view.size)
	cx, cy = view.ox + px, view.oy + py
	m = view.size * 1.5
	dx = 0.
	dy = 0.
	if cx < m:       dx = m - cx
	elif cx > w - m: dx = (w - m) - cx
	if cy < m:       dy = m - cy
	elif cy > h - m: dy = (h - m) - cy
	view.ox += dx
	view.oy += dy

def clear_selection(view):
	view.focus = None
	view.lo = None
	view.hi = None

def fmt_coord(view):
	f = view.focus
	if f is None: return ""
	lo = view.lo
	hi = view.hi
	if lo == hi:
		return f"({f[0]},{f[1]},{f[2]})"
	return f"({lo[0]},{lo[1]},{lo[2]}):({hi[0]},{hi[1]},{hi[2]})"

def selection_stats(view):
	# returns (total_count, list_of_numeric_values). formula cells are evaluated.
	nums = []
	count = 0
	for cell in selection_cells(view.focus, view.lo, view.hi):
		count += 1
		raw = view.cells.get(cell)
		if raw is None: continue
		v = cell_numeric_value(raw, cell, view.cells, view.eval_cache)
		if v is not None: nums.append(v)
	return count, nums

def fmt_stats(view):
	count, nums = selection_stats(view)
	parts = [f"Count={count}"]
	if nums:
		total = sum(nums)
		parts.append(f"Avg={total / len(nums):g}")
		parts.append(f"Min={min(nums):g}")
		parts.append(f"Max={max(nums):g}")
		parts.append(f"Sum={total:g}")
	return parts

def fmt_status_left(view):
	parts = ["Edit" if view.editing else "Ready"]
	if view.focus is not None:
		parts.append(fmt_coord(view))
		parts.extend(fmt_stats(view))
	return "   ".join(parts)

def fmt_status_right(view):
	zoom = int(round(view.size / HEX_SIZE * 100.))
	deg = math.degrees(view.angle) % 360.
	return f"{zoom}%   {deg:.1f}°"

def refresh_status(view):
	if view.left_label is None: return
	view.left_label.set_text(fmt_status_left(view))
	view.right_label.set_text(fmt_status_right(view))

def refresh_entry(view):
	if view.entry is None: return
	want = view.cells.get(view.focus, "") if view.focus is not None else ""
	if view.entry.get_text() != want:
		view._updating_entry = True
		view.entry.set_text(want)
		view._updating_entry = False

def dirty(view, area):
	# pure-view refresh; set_cell handles value invalidation.
	refresh_status(view)
	refresh_entry(view)
	area.queue_draw()

def extract_deps(raw, owner):
	# collect every cell referenced by this formula, resolving relative refs
	# against the owner. parse failures return the empty set; invalid refs
	# are already screened out at parse time via the sum=0 check.
	if not raw or not raw.startswith("="): return set()
	try: node = formula.parse(raw[1:])
	except formula.ParseError: return set()
	deps = set()
	stack = [node]
	while stack:
		n = stack.pop()
		if isinstance(n, formula.Rel):
			deps.add((owner[0] + n.dx, owner[1] + n.dy, owner[2] + n.dz))
		elif isinstance(n, formula.Abs):
			deps.add((n.x, n.y, n.z))
		elif isinstance(n, formula.BinOp):
			stack.append(n.left); stack.append(n.right)
		elif isinstance(n, formula.UnOp):
			stack.append(n.operand)
	return deps

def invalidate(view, cell):
	# invalidate this cell plus all transitive dependents in the eval cache.
	stack = [cell]
	seen = set()
	while stack:
		c = stack.pop()
		if c in seen: continue
		seen.add(c)
		view.eval_cache.pop(c, None)
		for d in view.dependents.get(c, ()):
			if d not in seen: stack.append(d)

def set_cell(view, cell, raw):
	# the single entry point for mutating view.cells. maintains the dep graph
	# and invalidates affected eval cache entries. raw of None or empty-string
	# removes the cell.
	if raw: view.cells[cell] = raw
	else:   view.cells.pop(cell, None)
	# rewire deps.
	old = view.deps.get(cell, set())
	for d in old:
		s = view.dependents.get(d)
		if s: s.discard(cell)
	new = extract_deps(raw, cell) if raw else set()
	if new: view.deps[cell] = new
	else:   view.deps.pop(cell, None)
	for d in new:
		view.dependents.setdefault(d, set()).add(cell)
	invalidate(view, cell)

def copy_to_clipboard(view):
	fx, fy, fz = view.focus
	cb = {}
	for cell in selection_cells(view.focus, view.lo, view.hi):
		if cell in view.cells:
			cb[(cell[0] - fx, cell[1] - fy, cell[2] - fz)] = view.cells[cell]
	view.clipboard = cb

def delete_selection_cells(view):
	for cell in selection_cells(view.focus, view.lo, view.hi):
		set_cell(view, cell, None)

def paste_from_clipboard(view):
	if not view.clipboard: return
	fx, fy, fz = view.focus
	for (dx, dy, dz), s in view.clipboard.items():
		set_cell(view, (fx + dx, fy + dy, fz + dz), s)

########################################################################################### main

def on_draw(area, ctx, w, h, view):
	draw(ctx, w, h, view)

def make_tristar_cursor():
	# render a 3-arm tristar to a cairo surface, wrap it as a Gdk.Texture, and
	# build a cursor with the hotspot at center. fixed orientation; the arms
	# don't rotate with the grid.
	size = 24
	c = size / 2.
	arm = 9.
	surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
	cx = cairo.Context(surf)
	cx.set_antialias(cairo.ANTIALIAS_BEST)
	# white halo first so the arms read on dark backgrounds too.
	cx.set_source_rgb(1., 1., 1.)
	cx.set_line_width(3.)
	cx.set_line_cap(cairo.LINE_CAP_ROUND)
	for a_deg in (90, 210, 330):
		a = math.radians(a_deg)
		cx.move_to(c, c)
		cx.line_to(c + arm * math.cos(a), c + arm * math.sin(a))
	cx.stroke()
	# black arms on top.
	cx.set_source_rgb(0., 0., 0.)
	cx.set_line_width(1.5)
	for a_deg in (90, 210, 330):
		a = math.radians(a_deg)
		cx.move_to(c, c)
		cx.line_to(c + arm * math.cos(a), c + arm * math.sin(a))
	cx.stroke()
	surf.flush()
	data = GLib.Bytes.new(bytes(surf.get_data()))
	texture = Gdk.MemoryTexture.new(
		size, size,
		Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
		data,
		surf.get_stride())
	return Gdk.Cursor.new_from_texture(texture, int(c), int(c), None)

def on_motion(controller, x, y, mouse, view, area):
	mouse.x = x
	mouse.y = y
	want_tristar = False
	if is_fillable(view):
		pt = selection_handle_point(view)
		if pt is not None:
			if math.hypot(x - pt[0], y - pt[1]) < FILL_HIT_RADIUS:
				want_tristar = True
	area.set_cursor(view.tristar_cursor if want_tristar else None)

def on_scroll(controller, dx, dy, view, area, mouse):
	state = controller.get_current_event_state()
	if state & Gdk.ModifierType.CONTROL_MASK:
		zoom_around(view, mouse.x, mouse.y, ZOOM_PER_SCROLL ** (-dy))
	else:
		rotate_around(view, mouse.x, mouse.y, ROT_PER_SCROLL * dy)
	dirty(view, area)
	return True

def on_pan_begin(gesture, start_x, start_y, view):
	view.pan_ox0 = view.ox
	view.pan_oy0 = view.oy

def on_pan_update(gesture, offset_x, offset_y, view, area):
	view.ox = view.pan_ox0 + offset_x
	view.oy = view.pan_oy0 + offset_y
	dirty(view, area)

def on_sel_begin(gesture, start_x, start_y, view, area):
	view.sel_sx0 = start_x
	view.sel_sy0 = start_y
	if is_fillable(view):
		pt = selection_handle_point(view)
		if pt is not None:
			hx, hy = pt
			if math.hypot(start_x - hx, start_y - hy) < FILL_HIT_RADIUS:
				view.filling = True
				view.fill_direction = None
				view.fill_steps = 0
				dirty(view, area)
				return
	view.filling = False
	cell = cell_at_pixel(start_x, start_y, view)
	view.focus = cell
	view.lo = cell
	view.hi = cell
	view.shift_sel = bool(gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
	view.editing = False
	dirty(view, area)

def on_sel_update(gesture, offset_x, offset_y, view, area):
	if view.filling:
		d, steps = fill_direction_and_steps(view, offset_x, offset_y)
		view.fill_direction = d
		view.fill_steps = steps
		dirty(view, area)
		return
	sx = view.sel_sx0 + offset_x
	sy = view.sel_sy0 + offset_y
	cell = cell_at_pixel(sx, sy, view)
	f = view.focus
	if view.shift_sel:
		# radial grow around focus by the hex distance to the pointer cell.
		r = hex_distance(f, cell)
		view.lo = (f[0] - r, f[1] - r, f[2] - r)
		view.hi = (f[0] + r, f[1] + r, f[2] + r)
	else:
		# asymmetric: lo/hi are the per-coord min/max of focus and pointer cell.
		view.lo = (min(f[0], cell[0]), min(f[1], cell[1]), min(f[2], cell[2]))
		view.hi = (max(f[0], cell[0]), max(f[1], cell[1]), max(f[2], cell[2]))
	dirty(view, area)

def on_sel_end(gesture, offset_x, offset_y, view, area):
	if not view.filling: return
	apply_fill(view)
	view.filling = False
	view.fill_direction = None
	view.fill_steps = 0
	dirty(view, area)

def apply_fill(view):
	new_cells, fills = fill_plan(view)
	if not fills: return
	for c, s in fills.items():
		set_cell(view, c, s)
	# extend lo/hi along the fill direction: push hi where d is positive and lo
	# where d is negative.
	d = view.fill_direction
	steps = view.fill_steps
	lo = list(view.lo)
	hi = list(view.hi)
	for i in range(3):
		if d[i] > 0: hi[i] += steps * d[i]
		elif d[i] < 0: lo[i] += steps * d[i]
	view.lo = tuple(lo)
	view.hi = tuple(hi)

ARROW_SCREEN_ANGLES = {
	Gdk.KEY_Right: 0.,
	Gdk.KEY_Down:  math.pi / 2.,
	Gdk.KEY_Left:  math.pi,
	Gdk.KEY_Up:   -math.pi / 2.,
}

def on_key_pressed(controller, keyval, keycode, state, view, area):
	if view.focus is None:
		return False
	shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
	ctrl  = bool(state & Gdk.ModifierType.CONTROL_MASK)
	# clipboard commands take precedence over the plain Delete/Insert handlers.
	if ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_Insert):
		copy_to_clipboard(view)
		return True
	if (ctrl and keyval == Gdk.KEY_x) or (shift and keyval == Gdk.KEY_Delete):
		copy_to_clipboard(view)
		delete_selection_cells(view)
		dirty(view, area)
		return True
	if (ctrl and keyval == Gdk.KEY_v) or (shift and keyval == Gdk.KEY_Insert):
		paste_from_clipboard(view)
		dirty(view, area)
		return True
	if keyval == Gdk.KEY_Escape:
		if view.editing:
			set_cell(view, view.focus, view.pre_edit)
			view.editing = False
		else:
			clear_selection(view)
		dirty(view, area)
		return True
	if keyval == Gdk.KEY_F2:
		if not view.editing:
			view.pre_edit = view.cells.get(view.focus, "")
			view.editing = True
		view.caret_pos = len(view.cells.get(view.focus, ""))
		dirty(view, area)
		return True
	# text-caret navigation inside the current cell (edit mode only, no shift).
	if view.editing and not shift:
		if keyval == Gdk.KEY_Left:
			if view.caret_pos > 0:
				view.caret_pos -= 1
				dirty(view, area)
			return True
		if keyval == Gdk.KEY_Right:
			if view.caret_pos < len(view.cells.get(view.focus, "")):
				view.caret_pos += 1
				dirty(view, area)
			return True
		if keyval == Gdk.KEY_Home:
			view.caret_pos = 0
			dirty(view, area)
			return True
		if keyval == Gdk.KEY_End:
			view.caret_pos = len(view.cells.get(view.focus, ""))
			dirty(view, area)
			return True
	if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
		move_cursor(view, neighbor_for_screen_direction(view, math.pi / 2.), shift, area)
		dirty(view, area)
		return True
	if keyval in ARROW_SCREEN_ANGLES:
		move_cursor(view, neighbor_for_screen_direction(view, ARROW_SCREEN_ANGLES[keyval]), shift, area)
		dirty(view, area)
		return True
	if keyval == Gdk.KEY_BackSpace:
		s = view.cells.get(view.focus, "")
		if not view.editing:
			view.pre_edit = s
			view.editing = True
			view.caret_pos = len(s)
		if view.caret_pos > 0 and s:
			new = s[:view.caret_pos - 1] + s[view.caret_pos:]
			view.caret_pos -= 1
			set_cell(view, view.focus, new)
			dirty(view, area)
		return True
	if keyval == Gdk.KEY_Delete:
		if view.editing:
			s = view.cells.get(view.focus, "")
			if view.caret_pos < len(s):
				new = s[:view.caret_pos] + s[view.caret_pos + 1:]
				set_cell(view, view.focus, new)
				dirty(view, area)
			return True
		delete_selection_cells(view)
		dirty(view, area)
		return True
	uch = Gdk.keyval_to_unicode(keyval)
	if uch == 0: return False
	c = chr(uch)
	if not c.isprintable(): return False
	if view.editing:
		s = view.cells.get(view.focus, "")
		set_cell(view, view.focus, s[:view.caret_pos] + c + s[view.caret_pos:])
		view.caret_pos += 1
	else:
		view.pre_edit = view.cells.get(view.focus, "")
		set_cell(view, view.focus, c)
		view.editing = True
		view.caret_pos = 1
	dirty(view, area)
	return True

def on_entry_changed(entry, view, area):
	if view._updating_entry: return
	if view.focus is None: return
	if not view.editing:
		view.pre_edit = view.cells.get(view.focus, "")
		view.editing = True
	set_cell(view, view.focus, entry.get_text())
	refresh_status(view)
	area.queue_draw()

def on_entry_activate(entry, view, area):
	# Enter in the entry commits and moves the cell cursor down.
	view.editing = False
	move_cursor(view, neighbor_for_screen_direction(view, math.pi / 2.), False, area)
	area.grab_focus()
	dirty(view, area)

def on_entry_key(controller, keyval, keycode, state, view, area):
	if keyval == Gdk.KEY_Escape:
		if view.editing:
			set_cell(view, view.focus, view.pre_edit)
			view.editing = False
		area.grab_focus()
		dirty(view, area)
		return True
	return False

def on_activate(app):
	view = View()
	mouse = Mouse()
	win = Gtk.ApplicationWindow(application=app, title="Hexcel")
	win.set_default_size(WIN_W, WIN_H)
	area = Gtk.DrawingArea()
	area.set_hexpand(True)
	area.set_vexpand(True)
	area.set_focusable(True)
	area.set_focus_on_click(True)
	area.set_draw_func(on_draw, view)
	# formula bar at the top.
	view.entry = Gtk.Entry(hexpand=True, margin_start=8, margin_end=8,
			margin_top=4, margin_bottom=4)
	view.entry.connect("changed", on_entry_changed, view, area)
	view.entry.connect("activate", on_entry_activate, view, area)
	entry_key = Gtk.EventControllerKey()
	entry_key.connect("key-pressed", on_entry_key, view, area)
	view.entry.add_controller(entry_key)
	# status bar below the hex grid.
	view.left_label = Gtk.Label(xalign=0., hexpand=True)
	view.right_label = Gtk.Label(xalign=1.)
	status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
			margin_start=8, margin_end=8, margin_top=4, margin_bottom=4)
	status.append(view.left_label)
	status.append(view.right_label)
	root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
	root.append(view.entry)
	root.append(area)
	root.append(status)
	scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
	scroll.connect("scroll", on_scroll, view, area, mouse)
	area.add_controller(scroll)
	view.tristar_cursor = make_tristar_cursor()
	motion = Gtk.EventControllerMotion()
	motion.connect("motion", on_motion, mouse, view, area)
	motion.connect("enter", on_motion, mouse, view, area)
	area.add_controller(motion)
	pan = Gtk.GestureDrag()
	pan.set_button(2)
	pan.connect("drag-begin", on_pan_begin, view)
	pan.connect("drag-update", on_pan_update, view, area)
	area.add_controller(pan)
	sel = Gtk.GestureDrag()
	sel.set_button(1)
	sel.connect("drag-begin", on_sel_begin, view, area)
	sel.connect("drag-update", on_sel_update, view, area)
	sel.connect("drag-end", on_sel_end, view, area)
	area.add_controller(sel)
	# key controller on area so entry focus naturally captures entry's own keys.
	key = Gtk.EventControllerKey()
	key.connect("key-pressed", on_key_pressed, view, area)
	area.add_controller(key)
	win.set_child(root)
	refresh_status(view)
	refresh_entry(view)
	win.present()
	area.grab_focus()

if __name__ == "__main__":
	app = Gtk.Application(application_id="de.tensor.hexcel")
	app.connect("activate", on_activate)
	app.run(None)
