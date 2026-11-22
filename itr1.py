"""v4: v1's proven scoring + better mechanics (evasive movement, beam dodge,
reactive jumping, sound awareness, cover system, obstacle avoidance)."""
import json
import math
import os
import random
import mechfight as mf


# =========================================================================
# ARENA MAP
# =========================================================================
class ArenaMap:
    CELL = 40

    def __init__(self, hello_map=None):
        self.prisms = []
        self.grid = {}
        self.loaded = False
        self._load(self._find_world(hello_map))

    @staticmethod
    def _find_world(hello_map):
        """Use the map dir the server tells us about; fall back to the old hardcoded path."""
        cands = []
        if hello_map:
            d = hello_map.get("dir", "")
            for f in hello_map.get("files", []):
                if f.endswith("world.json"):
                    cands.append(f if f.startswith("/") else os.path.join(d, f))
            cands.append(os.path.join(d, "world.json"))
        cands += ["/arena/v2/world.json", "/arena/world.json"]
        for p in cands:
            if p and os.path.exists(p):
                return p
        return None

    def _load(self, path):
        if not path or not os.path.exists(path):
            mf.log(f"[arena] world.json not found; cover/LOS disabled")
            return
        try:
            with open(path) as f:
                world = json.load(f)
        except Exception:
            return
        for p in world.get("prisms", []):
            ring = p.get("ring", [])
            if len(ring) < 6:
                continue
            verts = [(ring[i], ring[i + 1]) for i in range(0, len(ring), 2)]
            base = p.get("base", 0)
            top = p.get("top", 50)
            if top - base < 2.5:
                continue
            xs = [v[0] for v in verts]
            ys = [v[1] for v in verts]
            if p.get("kind") == "bridge":
                continue                    # walk and shoot under bridges
            rec = dict(verts=verts, cx=sum(xs)/len(xs), cy=sum(ys)/len(ys),
                       base=base, top=top,
                       min_x=min(xs), max_x=max(xs), min_y=min(ys), max_y=max(ys))
            idx = len(self.prisms)
            self.prisms.append(rec)
            for gx in range(int(rec["min_x"] // self.CELL) - 1,
                            int(rec["max_x"] // self.CELL) + 2):
                for gy in range(int(rec["min_y"] // self.CELL) - 1,
                                int(rec["max_y"] // self.CELL) + 2):
                    self.grid.setdefault((gx, gy), []).append(idx)
        self.loaded = bool(self.prisms)
        mf.log(f"[arena] loaded {len(self.prisms)} prisms from {path}")

    def nearby(self, x, y, radius=60):
        seen = set()
        r2 = radius * radius
        for gx in range(int((x - radius) // self.CELL),
                        int((x + radius) // self.CELL) + 1):
            for gy in range(int((y - radius) // self.CELL),
                            int((y + radius) // self.CELL) + 1):
                for idx in self.grid.get((gx, gy), []):
                    if idx in seen:
                        continue
                    seen.add(idx)
                    p = self.prisms[idx]
                    dx = max(p["min_x"] - x, 0, x - p["max_x"])
                    dy = max(p["min_y"] - y, 0, y - p["max_y"])
                    if dx * dx + dy * dy < r2:
                        yield p

    def nearest_cover(self, my_pos, enemy_pos, max_dist=55,
                      zone_center=None, zone_radius=None):
        mx, my_ = my_pos[0], my_pos[1]
        ex, ey = enemy_pos[0], enemy_pos[1]
        best, best_sc = None, float("inf")

        # how far WE are from zone center (for penalizing cover that's farther out)
        my_zone_dist = 0.0
        if zone_center:
            my_zone_dist = math.hypot(mx - zone_center[0], my_ - zone_center[1])

        for prism in self.nearby(mx, my_, max_dist):
            pcx, pcy = prism["cx"], prism["cy"]
            verts = prism["verts"]
            n = len(verts)
            for i in range(n):
                ax, ay = verts[i]
                bx, by = verts[(i + 1) % n]
                emx, emy = (ax + bx) * 0.5, (ay + by) * 0.5
                if (emx - pcx) * (ex - pcx) + (emy - pcy) * (ey - pcy) > 0:
                    continue
                sx, sy = bx - ax, by - ay
                sl2 = sx * sx + sy * sy
                if sl2 < 0.01:
                    continue
                t = max(0.0, min(1.0, ((mx - ax) * sx + (my_ - ay) * sy) / sl2))
                px, py = ax + t * sx, ay + t * sy
                ox, oy = px - pcx, py - pcy
                ol = math.hypot(ox, oy)
                if ol < 0.1:
                    continue
                cx_, cy_ = px + ox / ol * 3.0, py + oy / ol * 3.0

                # hard filter: cover must be inside 92% of next zone
                if zone_center and zone_radius:
                    if math.hypot(cx_ - zone_center[0],
                                  cy_ - zone_center[1]) > zone_radius * 0.92:
                        continue

                # scoring: distance to me + penalty for running away from zone
                dist = math.hypot(cx_ - mx, cy_ - my_)
                if zone_center:
                    cover_zone_dist = math.hypot(cx_ - zone_center[0],
                                                  cy_ - zone_center[1])
                    # penalize cover that's farther from zone center than we are
                    if cover_zone_dist > my_zone_dist:
                        dist += (cover_zone_dist - my_zone_dist) * 0.8
                if dist < best_sc:
                    best_sc, best = dist, (cx_, cy_)
        return best

    def is_blocked(self, x, y):
        for p in self.nearby(x, y, 5):
            if self._pip(x, y, p["verts"]):
                return True
        return False

    def has_los(self, fx, fy, tx, ty):
        mid_x, mid_y = (fx + tx) * 0.5, (fy + ty) * 0.5
        rad = math.hypot(tx - fx, ty - fy) * 0.5 + 10
        for p in self.nearby(mid_x, mid_y, rad):
            verts = p["verts"]
            n = len(verts)
            for i in range(n):
                if self._seg_x(fx, fy, tx, ty,
                               verts[i][0], verts[i][1],
                               verts[(i + 1) % n][0], verts[(i + 1) % n][1]):
                    return False
        return True

    def steer_around(self, mx, my_, tx, ty, step=6.0):
        dx, dy = tx - mx, ty - my_
        d = math.hypot(dx, dy)
        if d < 1:
            return None
        ndx, ndy = dx / d, dy / d
        chk = min(step, d)
        if not self.is_blocked(mx + ndx * chk, my_ + ndy * chk):
            return None
        for angle in (0.6, -0.6, 1.1, -1.1, 1.6, -1.6):
            ca, sa = math.cos(angle), math.sin(angle)
            sx_ = ndx * ca - ndy * sa
            sy_ = ndx * sa + ndy * ca
            if not self.is_blocked(mx + sx_ * step, my_ + sy_ * step):
                return (sx_, sy_)
        return None

    @staticmethod
    def _pip(x, y, verts):
        inside = False
        n = len(verts)
        j = n - 1
        for i in range(n):
            xi, yi = verts[i]; xj, yj = verts[j]
            if ((yi > y) != (yj > y)) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _seg_x(ax, ay, bx, by, cx, cy, dx, dy):
        def _c(ox, oy, px, py, qx, qy):
            return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)
        d1, d2 = _c(cx, cy, dx, dy, ax, ay), _c(cx, cy, dx, dy, bx, by)
        d3, d4 = _c(ax, ay, bx, by, cx, cy), _c(ax, ay, bx, by, dx, dy)
        return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


# =========================================================================
# BOT
# =========================================================================
class Bot:
    name = "ghost-v5"

    def __init__(self, hello: mf.Hello):
        self.config = hello["config"]
        self.reach = self.config["loot"]["pickup_radius"]
        self.rng = random.Random(hello["seed"])
        self.weapon_cfgs = self.config["weapons"]
        # lightning: ~240 dps inside 45 m (60 dmg / 0.25 s) and shorts lasers;
        # laser: ~130 dps at 280 m; mortar: area denial, weak vs movers.
        self.weapon_prio = {"lightning": 4, "laser": 3, "mortar": 2, "gun": 1}
        self.beats = {"laser": "mortar", "mortar": "lightning", "lightning": "laser"}
        self.upgrade_defs = {u["id"]: u for u in self.config.get("upgrades", [])}
        self.gravity = abs(self.weapon_cfgs.get("mortar", {}).get("gravity", 22.0))
        self.rear_mult = self.config.get("mech", {}).get("rear_mult", 1.0)

        self.arena = ArenaMap(hello.get("map"))

        # v5 state
        self.behavior = "zone"            # hysteresis: last chosen behavior
        self.hit_bearing = None            # yaw toward the last thing that hurt us
        self.hit_t = -999.0
        self.last_dealt_t = -999.0       # for regen timer (dealing damage resets it)
        self.hit_by = None
        self.mem = {}                      # enemy id -> (pos, vel, tick) last seen
        self._cache_tick = None
        self._cache_enemies = []
        self._cache_npcs = []
        self.timing = []
        self._last_move = None

        self.strafe_dir = 1
        self.strafe_ticks = 0
        self.strafe_flip_at = self.rng.randint(5, 12)
        self.last_pos = None
        self.stuck_ticks = 0
        self.kill_count = 0

        # v2 additions
        self.dmg_ticks = []
        self.move_phase = 0.0
        self.dodge_amplitude = self.rng.uniform(0.3, 0.6)

        # cover cache
        self._cover_pt = None
        self._cover_tick = -99

    # =====================================================================
    # VECTOR MATH
    # =====================================================================
    def _d2(self, a, b):  return math.dist(a[:2], b[:2])
    def _d3(self, a, b):  return math.dist(a[:3], b[:3])
    def _yaw(self, s, d): return math.atan2(d[1] - s[1], d[0] - s[0])

    def _n2(self, v):
        L = math.hypot(v[0], v[1])
        return [v[0] / L, v[1] / L] if L > 1e-4 else [0.0, 0.0]

    def _c2(self, v, cap=1.0):
        L = math.hypot(v[0], v[1])
        return [v[0] / L * cap, v[1] / L * cap] if L > cap else v

    def _blend(self, a, b, wa=0.5, wb=0.5):
        return self._c2([a[0] * wa + b[0] * wb, a[1] * wa + b[1] * wb])

    def _toward(self, s, d):
        y = self._yaw(s, d)
        return [math.cos(y), math.sin(y)]

    def _perp(self, v, d=1):
        return [-v[1] * d, v[0] * d]

    # =====================================================================
    # QUERIES
    # =====================================================================
    def _refresh_cache(self, obs):
        if self._cache_tick == obs["tick"]:
            return
        me = obs["self"]
        key = lambda m: self._d3(m["pos"], me["pos"])
        self._cache_enemies = sorted((m for m in obs["mechs"] if m["kind"] == "mech"), key=key)
        self._cache_npcs = sorted((m for m in obs["mechs"] if m["kind"] == "npc"), key=key)
        self._cache_tick = obs["tick"]

    def _enemies(self, obs):
        self._refresh_cache(obs)
        return self._cache_enemies

    def _npcs(self, obs):
        self._refresh_cache(obs)
        return self._cache_npcs

    def _ehp(self, e):
        """Enemy hp fraction; hp is optional in the protocol (vision.show_enemy_hp)."""
        hp = e.get("hp")
        if hp is None:
            return 1.0
        return hp / max(e.get("max_hp") or self.config.get("mech", {}).get("hp", 1400), 1.0)

    def _wrange(self, w, default):
        st = (w or {}).get("stats") or {}
        return st.get("range", default)

    def _primary(self, me):
        """Best weapon we hold (what we aim/lead for)."""
        best = None
        for w in me["weapons"]:
            if w and (best is None or self.weapon_prio.get(w["kind"], 0) >
                      self.weapon_prio.get(best["kind"], 0)):
                best = w
        return best

    def _zone_margin(self, obs):
        return obs["zone"]["radius"] - self._d2(obs["self"]["pos"], obs["zone"]["center"])

    def _hp(self, me):
        return me["hp"] / max(me["max_hp"], 1.0)

    def _nearby_count(self, obs, r):
        me = obs["self"]
        return sum(1 for e in self._enemies(obs) if self._d2(me["pos"], e["pos"]) < r)

    def _next_up_cost(self, me):
        t = self.config["cubes"]["upgrade_thresholds"]
        n = len(me["upgrades"])
        return t[n] if n < len(t) else float("inf")

    def _w2(self, me):
        """Our best non-gun weapon kind, whichever slot it landed in."""
        p = self._primary(me)
        return p["kind"] if p and p["kind"] != "gun" else None

    def _recent_dps(self, obs):
        now = obs["t"]
        self.dmg_ticks = [t for t in self.dmg_ticks if now - t < 1.0]
        return len(self.dmg_ticks)

    def _recent_damage_events(self, obs):
        """Count damage events THIS tick (for scoring, like v1)."""
        return sum(1 for e in obs.get("events", []) if e.get("type") == "damaged")

    def _sound_threat(self, obs):
        near = sum(1 for s in obs["sounds"]
                   if s["dist"] == "near" and
                   s["kind"] in ("step", "gun", "mortar", "laser", "lightning"))
        mid = sum(1 for s in obs["sounds"]
                  if s["dist"] == "mid" and s["kind"] in ("step", "gun"))
        return near + mid * 0.5

    def _matchup_bonus(self, me, enemy):
        my2 = self._w2(me) or "gun"
        their = enemy.get("weapons") or []
        their2 = next((k for k in their if k != "gun"), their[0] if their else "gun")
        if self.beats.get(my2) == their2:
            return 12.0
        if self.beats.get(their2) == my2:
            return -8.0
        return 0.0

    def _threat_dir(self, obs):
        enemies = self._enemies(obs)
        if not enemies:
            return None
        me = obs["self"]
        fx, fy = 0.0, 0.0
        for e in enemies[:5]:
            d = max(self._d2(me["pos"], e["pos"]), 1.0)
            w = 1.0 / (d * d)
            fx += (e["pos"][0] - me["pos"][0]) * w
            fy += (e["pos"][1] - me["pos"][1]) * w
        L = math.hypot(fx, fy)
        return [fx / L, fy / L] if L > 1e-4 else None

    def _behind_cover(self, obs):
        if not self.arena.loaded:
            return False
        enemies = self._enemies(obs)
        if not enemies:
            return True
        me = obs["self"]
        return not self.arena.has_los(me["pos"][0], me["pos"][1],
                                      enemies[0]["pos"][0], enemies[0]["pos"][1])

    def _get_cover(self, obs):
        tick = obs["tick"]
        if self._cover_pt and tick - self._cover_tick < 5:
            return self._cover_pt
        if not self.arena.loaded:
            self._cover_pt = None
            return None
        enemies = self._enemies(obs)
        if not enemies:
            self._cover_pt = None
            self._cover_tick = tick
            return None
        me = obs["self"]
        # weighted enemy centroid
        wx, wy, wt = 0.0, 0.0, 0.0
        for e in enemies[:4]:
            d = max(self._d2(me["pos"], e["pos"]), 1.0)
            w = 1.0 / d
            wx += e["pos"][0] * w; wy += e["pos"][1] * w; wt += w
        if wt < 1e-6:
            self._cover_pt = None
            self._cover_tick = tick
            return None
        zc = obs["zone"].get("next_center", obs["zone"]["center"])
        zr = obs["zone"].get("next_radius", obs["zone"]["radius"])
        self._cover_pt = self.arena.nearest_cover(
            me["pos"], (wx / wt, wy / wt),
            max_dist=55, zone_center=zc, zone_radius=zr)
        self._cover_tick = tick
        return self._cover_pt

    # =====================================================================
    # UPGRADE — more survival-biased than v1
    # =====================================================================
    def _choose_upgrade(self, offer, me):
        """Kill-oriented: points come from enemies dying, so damage output on the
        weapon we actually hold ranks highest, then durability, then mobility."""
        hp = self._hp(me)
        held = {w["kind"] for w in me["weapons"] if w}
        prios = {
            "damage": 9, "dmg": 9, "rate": 7, "cooldown": 7, "charge": 6,
            "hp": 7, "health": 7, "armor": 7, "shield": 6,
            "repair": 5, "regen": 5, "speed": 4, "move": 4,
            "range": 4, "energy": 3, "reload": 3, "ammo": 3,
            "turn": 3, "sensor": 2, "vision": 2,
        }
        if hp < 0.5:
            prios["hp"] = 11; prios["health"] = 11; prios["repair"] = 9
        best_i, best_s = 0, -1e9
        for i, ch in enumerate(offer["choices"]):
            d = self.upgrade_defs.get(ch.get("id"), {})
            req = d.get("requires")
            if req and req not in held:
                s = -5.0       # buffs a weapon we don't carry: nearly useless
            else:
                paths = " ".join(e.get("path", "") for e in d.get("effects", []))
                txt = (ch.get("name", "") + " " + ch.get("desc", "") + " " +
                       ch.get("id", "") + " " + paths).lower()
                s = sum(v for k, v in prios.items() if k in txt)
                if req:        # weapon-specific buff for a weapon we hold: bonus
                    s += 3
            if s > best_s:
                best_s, best_i = s, i
        return best_i

    # =====================================================================
    # SCORING — based on v1 (which scored 1.36), with targeted tweaks
    # =====================================================================

    def _score_zone(self, obs):
        """SAME as v1."""
        me = obs["self"]
        margin = self._zone_margin(obs)
        score = 5.0
        if not me["in_zone"]:
            score += 100.0
        if margin < 5:
            score += 80.0
        elif margin < 15:
            score += 45.0
        elif margin < 30:
            score += 20.0
        t = obs.get("t", 0)
        if t > obs["zone"].get("shrink_start", 60) and margin < 40:
            score += 15.0
        return score

    def _target_hp(self, obs):
        tgt = self._best_fight_target(obs)
        return self._ehp(tgt) if tgt else 1.0

    def _recently_hit(self, obs, window=1.5):
        """window in match seconds"""
        return obs["t"] - self.hit_t < window

    def _score_fight(self, obs):
        """Points in practice come from house bots dying, and they never fight each
        other, so we must win duels. Commit to 1v1s; only back off when outnumbered
        or clearly losing."""
        enemies = self._enemies(obs)
        me = obs["self"]
        hp = self._hp(me)
        if not enemies:
            # shot by something we can't see: turn and fight it, don't run blind
            if self._recently_hit(obs) and self.hit_bearing is not None:
                return 55.0 + hp * 20.0
            return -50.0
        nearest_d = self._d2(me["pos"], enemies[0]["pos"])
        nearby = self._nearby_count(obs, 50)
        thp = self._target_hp(obs)

        score = 40.0 + hp * 30.0
        if self._w2(me):
            score += 10.0
        score += min(me["cubes"], 10) * 1.0
        score += self._matchup_bonus(me, enemies[0])
        score += (hp - thp) * 30.0          # finish weak targets, avoid fresh ones when hurt

        if nearby <= 1:
            score += 25.0                   # lone hunter: this is where points come from
        else:
            score -= (nearby - 1) * 20.0
        if nearest_d > 90:
            score -= 15.0
        if hp < 0.25 and thp > hp:
            score -= 40.0
        if thp < 0.3:
            score += 15.0                   # about to get a kill, don't let go
        return score

    def _score_flee(self, obs):
        """Only when outnumbered or losing badly; fleeing exposes our back (rear_mult)."""
        me = obs["self"]
        hp = self._hp(me)
        nearby = self._nearby_count(obs, 40)
        score = (1.0 - hp) * 60.0 - hp * 20.0
        score += max(0, nearby - 1) * 22.0
        if nearby == 0:
            score -= 30.0
        if me["energy"] < 20:
            score += 5.0
        if nearby >= 2 and self.arena.loaded and self._get_cover(obs) is None:
            score -= 25.0                   # nowhere to break LOS: last stand instead
        return score

    def _score_cover(self, obs):
        """Break line of sight when outnumbered or hurt; not for every 1v1."""
        enemies = self._enemies(obs)
        if not enemies or not self.arena.loaded:
            return -50.0
        me = obs["self"]
        margin = self._zone_margin(obs)
        if margin < 15 or not me["in_zone"]:
            return -50.0
        if self._behind_cover(obs):
            return -10.0
        nearby = self._nearby_count(obs, 45)
        hp = self._hp(me)
        if nearby < 2 and hp > 0.4:
            return -30.0
        cover = self._get_cover(obs)
        if cover is None:
            return -40.0
        cover_dist = math.hypot(cover[0] - me["pos"][0], cover[1] - me["pos"][1])
        score = 20.0 + nearby * 14.0 + (1.0 - hp) * 30.0
        if self._recently_hit(obs, 1.0):
            score += 10.0
        if cover_dist < 15:
            score += 12.0
        elif cover_dist > 40:
            score -= 15.0
        return score

    def _any_enemy_los(self, obs):
        """obs.mechs includes enemies the server still tracks for memory_s after
        they leave view, so check real line of sight ourselves."""
        me = obs["self"]
        for e in self._enemies(obs)[:6]:
            if self._d2(me["pos"], e["pos"]) > 230:
                continue
            if not self.arena.loaded or self.arena.has_los(
                    me["pos"][0], me["pos"][1], e["pos"][0], e["pos"][1]):
                return True
        return False

    def _score_recover(self, obs):
        """Out of sight and hurt: stop shooting anything (dealing damage resets the
        5 s regen timer), stay quiet, heal at regen_per_s. This is how you survive
        being ganked: break LOS, heal, take them as they come around the corner."""
        me = obs["self"]
        hp = self._hp(me)
        if hp > 0.85 or not me["in_zone"] or self._zone_margin(obs) < 10:
            return -60.0
        if self._recently_hit(obs, 2.0) or self._any_enemy_los(obs):
            return -60.0
        return 35.0 + (1.0 - hp) * 90.0

    def _score_drop(self, obs):
        """Supply drops give cubes + repair. Worth it when nobody is on us."""
        drops = [d for d in obs.get("drops", []) if d.get("lands_in", 1) <= 3]
        if not drops:
            return -60.0
        me = obs["self"]
        d = min(drops, key=lambda x: self._d2(me["pos"], x["pos"]))
        dist = self._d2(me["pos"], d["pos"])
        if dist > 120:
            return -40.0
        if d.get("holder") not in (None, me["id"]):
            return -40.0
        score = 45.0 + (1.0 - self._hp(me)) * 40.0 - dist * 0.35
        score -= self._nearby_count(obs, 45) * 30.0
        if not obs["self"]["in_zone"] or math.hypot(
                d["pos"][0] - obs["zone"]["next_center"][0],
                d["pos"][1] - obs["zone"]["next_center"][1]) > obs["zone"]["next_radius"]:
            score -= 30.0
        return score

    def _score_loot_weapon(self, obs):
        """SAME as v1."""
        me = obs["self"]
        if self._w2(me):
            return -20.0
        picks = [p for p in obs["pickups"] if p["kind"] != "gun"]
        if not picks:
            return -30.0
        mep = me["pos"]
        nearby_pick = [p for p in picks if self._d2(mep, p["pos"]) < 80]
        if not nearby_pick:
            return -25.0
        best = max(nearby_pick, key=lambda p: self.weapon_prio.get(p["kind"], 0))
        best_d = self._d2(mep, best["pos"])
        score = 70.0 + self.weapon_prio.get(best["kind"], 0) * 10.0
        score -= best_d * 0.4
        enemies_close = self._nearby_count(obs, 30)
        if enemies_close == 0:
            score += 18.0
        else:
            score -= enemies_close * 12.0
        if obs.get("t", 0) < 90:
            score += 15.0
        return score

    def _score_loot_cubes(self, obs):
        """SAME as v1."""
        me = obs["self"]
        need = me["cubes"] < self._next_up_cost(me)
        mep = me["pos"]
        targets = list(obs["cubes"]) + [c for c in obs["crates"] if c.get("hp", 0) > 0]
        if not targets:
            return -40.0
        best_d = min(self._d2(mep, t["pos"]) for t in targets)
        score = (25.0 if need else 8.0) + 20.0 / max(best_d, 1.0)
        score -= self._nearby_count(obs, 35) * 10.0
        if obs.get("t", 0) > 200:
            score *= 0.6
        return score

    def _score_hunt_npc(self, obs):
        """SAME as v1."""
        npcs = self._npcs(obs)
        if not npcs:
            return -60.0
        me = obs["self"]
        t = obs.get("t", 0)
        if t > 180:
            return -40.0
        d = self._d2(me["pos"], npcs[0]["pos"])
        score = 35.0 * (1.0 - t / 420.0)
        if me["cubes"] < 3:
            score += 25.0
        score -= d * 0.35
        score -= self._nearby_count(obs, 45) * 18.0
        return score

    # =====================================================================
    # MOVEMENT — UPGRADED from v1
    # =====================================================================
    def _evasive_move(self, obs, base_dir):
        """Sinusoidal dodge overlay — replaces v1's fixed strafe."""
        self.strafe_ticks += 1
        if self.strafe_ticks >= self.strafe_flip_at:
            self.strafe_dir *= -1
            self.strafe_ticks = 0
            self.strafe_flip_at = self.rng.randint(4, 10)

        self.move_phase += 0.15 + self.rng.uniform(-0.03, 0.03)
        sin_off = math.sin(self.move_phase) * self.dodge_amplitude

        if base_dir is None:
            base_dir = [0.0, 1.0]
        perp = self._perp(base_dir, self.strafe_dir)
        return self._c2([base_dir[0] * 0.55 + perp[0] * sin_off,
                         base_dir[1] * 0.55 + perp[1] * sin_off])

    def _dodge_dir(self, obs):
        """v1 projectile dodge + beam dodge + laser charge dodge."""
        me = obs["self"]
        mx, my_ = me["pos"][0], me["pos"][1]

        # projectiles
        best_threat, best_dot = None, 0.0
        for p in obs["projectiles"]:
            dx, dy = mx - p["pos"][0], my_ - p["pos"][1]
            d = math.hypot(dx, dy)
            if d > 35 or d < 1:
                continue
            vl = math.hypot(p["vel"][0], p["vel"][1])
            if vl < 0.1:
                continue
            dot = (dx * p["vel"][0] + dy * p["vel"][1]) / (d * vl)
            if dot > 0.5 and dot > best_dot:
                best_dot, best_threat = dot, p
        if best_threat:
            perp = self._n2([-best_threat["vel"][1], best_threat["vel"][0]])
            return [perp[0] * self.strafe_dir, perp[1] * self.strafe_dir]

        # laser charges aimed at us
        for m in obs["mechs"]:
            if m["kind"] != "mech" or not m.get("charging"):
                continue
            if self._d2(me["pos"], m["pos"]) > 60:
                continue
            yaw = m["aim"][0]
            adx, ady = math.cos(yaw), math.sin(yaw)
            to_me = self._n2([mx - m["pos"][0], my_ - m["pos"][1]])
            if adx * to_me[0] + ady * to_me[1] > 0.7:
                return self._n2([-ady, adx])

        # fresh beams
        for b in obs.get("beams", []):
            if b["age"] > 0.3 or len(b["path"]) < 2:
                continue
            p0, p1 = b["path"][0], b["path"][-1]
            bx, by = p1[0] - p0[0], p1[1] - p0[1]
            bl = math.hypot(bx, by)
            if bl < 0.1:
                continue
            ts = ((mx - p0[0]) * bx + (my_ - p0[1]) * by) / (bl * bl)
            if 0 < ts < 1:
                cx_ = p0[0] + bx * ts
                cy_ = p0[1] + by * ts
                if math.hypot(mx - cx_, my_ - cy_) < 8:
                    return self._n2([-by * self.strafe_dir, bx * self.strafe_dir])
        return None

    def _apply_dodge(self, obs, move, boost=False):
        dodge = self._dodge_dir(obs)
        margin = self._zone_margin(obs)
        if dodge:
            if margin < 8 or not obs["self"]["in_zone"]:
                zone = self._toward(obs["self"]["pos"], obs["zone"]["next_center"])
                move = self._blend(dodge, zone, 0.55, 0.45)
            else:
                move = dodge
            boost = True
        return move, boost

    def _apply_obstacle_avoidance(self, obs, move):
        if not self.arena.loaded:
            return move
        me = obs["self"]
        steer = self.arena.steer_around(
            me["pos"][0], me["pos"][1],
            me["pos"][0] + move[0] * 6, me["pos"][1] + move[1] * 6)
        if steer:
            return self._c2([steer[0], steer[1]])
        return move

    def _zone_goal(self, obs):
        """Enemy-aware zone positioning (upgraded from v1)."""
        me = obs["self"]
        c = obs["zone"].get("next_center", obs["zone"]["center"])
        r = obs["zone"].get("next_radius", obs["zone"]["radius"])
        threat = self._threat_dir(obs)
        if threat and r > 10:
            away = [-threat[0], -threat[1]]
            target_r = r * 0.5
            goal = [c[0] + away[0] * target_r, c[1] + away[1] * target_r]
            if self.arena.loaded and self.arena.is_blocked(goal[0], goal[1]):
                steer = self.arena.steer_around(me["pos"][0], me["pos"][1],
                                                 goal[0], goal[1])
                if steer:
                    goal = [me["pos"][0] + steer[0] * target_r,
                            me["pos"][1] + steer[1] * target_r]
            return goal + [me["pos"][2]]
        # fallback: v1 style
        dx = me["pos"][0] - c[0]
        dy = me["pos"][1] - c[1]
        d = math.hypot(dx, dy)
        if d < 1:
            a = self.rng.uniform(0, 2 * math.pi)
            dx, dy, d = math.cos(a), math.sin(a), 1.0
        s = r * 0.6 / d
        return [c[0] + dx * s, c[1] + dy * s, me["pos"][2]]

    def _flee_dir(self, obs, enemies):
        me = obs["self"]
        fx, fy = 0.0, 0.0
        for e in enemies[:4]:
            d = max(self._d2(me["pos"], e["pos"]), 1.0)
            w = 1.0 / d                                # v1: same (linear, not inverse-square)
            fx += (me["pos"][0] - e["pos"][0]) * w
            fy += (me["pos"][1] - e["pos"][1]) * w
        flee = self._n2([fx, fy])
        zone_dir = self._toward(me["pos"], self._zone_goal(obs))
        margin = self._zone_margin(obs)

        # NEW: blend toward cover if available AND zone isn't pinching us
        if margin > 25:
            cover = self._get_cover(obs)
            if cover:
                cover_dir = self._toward(me["pos"], cover)
                flee = self._blend(flee, cover_dir, 0.5, 0.5)

        if margin < 25:
            return self._blend(flee, zone_dir, 0.35, 0.65)
        return self._blend(flee, zone_dir, 0.65, 0.35)

    # =====================================================================
    # COMBAT — v1 base + gun range/ammo limits
    # =====================================================================
    def _proj_speed(self, kind):
        if kind in ("laser", "lightning"):
            return 1e9
        return self.weapon_cfgs.get(kind, {}).get("speed", 80.0)

    def _target_z(self, tgt, kind):
        """Point to aim at: mech center of mass (scaled by cubes), NPC center,
        or near the feet for mortar splash."""
        z = tgt["pos"][2]
        if tgt.get("kind") == "mech":
            scale = 1.0 + 0.05 * (tgt.get("cubes") or 0)
            return z + (1.0 if kind == "mortar" else 4.5 * scale)
        if tgt.get("kind") == "npc":
            return z + 1.75
        return z + 0.5

    def _lead(self, me, tgt, kind=None):
        if kind is None:
            p = self._primary(me)
            kind = p["kind"] if p else "gun"
        dist = self._d2(me["pos"], tgt["pos"])
        t = dist / max(self._proj_speed(kind), 1.0)
        if kind == "mortar":
            t *= 1.3                        # arcing shot flies longer than straight-line
        vel = tgt.get("vel") or [0.0, 0.0, 0.0]
        return [tgt["pos"][0] + vel[0] * t,
                tgt["pos"][1] + vel[1] * t,
                self._target_z(tgt, kind) + vel[2] * t * 0.5]

    def _muzzle_z(self, me, kind):
        st = me.get("stats") or {}
        mz = st.get("muzzle_height", 7.0)
        if kind == "mortar":
            mz = mz * 9.5 / 7.0             # tube sits at height + 0.5
        return me["pos"][2] + mz

    def _aim_at(self, me, pos, kind=None):
        """pos is the point to hit (already at the right height)."""
        yaw = self._yaw(me["pos"], pos)
        hd = self._d2(me["pos"], pos)
        dz = pos[2] - self._muzzle_z(me, kind)
        pitch = math.atan2(dz, max(hd, 0.1))
        if kind == "mortar" and self.gravity > 0:
            v = self._proj_speed("mortar")
            g = self.gravity
            disc = v ** 4 - g * (g * hd * hd + 2 * dz * v * v)
            if disc >= 0 and hd > 0.5:
                pitch = math.atan((v * v - math.sqrt(disc)) / (g * hd))   # low arc
            else:
                pitch = math.radians(45)    # out of range: max-distance angle
        return [yaw, pitch]

    def _aim_for_fight(self, me, tgt):
        p = self._primary(me)
        kind = p["kind"] if p else "gun"
        pred = self._lead(me, tgt, kind)
        return self._aim_at(me, pred, kind), pred

    def _best_fight_target(self, obs):
        """Prefer weak + close targets; strongly prefer whoever is shooting us."""
        me = obs["self"]
        candidates = []
        for e in self._enemies(obs):
            d = self._d2(me["pos"], e["pos"])
            if d > 120:
                continue
            s = (1.0 - self._ehp(e)) * 40 - d * 0.15
            if self.hit_by is not None and e.get("id") == self.hit_by and \
               self._recently_hit(obs, 2.0):
                s += 15.0
            candidates.append((s, e))
        if not candidates:
            enemies = self._enemies(obs)
            return enemies[0] if enemies else None
        return max(candidates, key=lambda x: x[0])[1]

    def _fire_slots(self, me, dist, aim_err=0.0):
        """Per-weapon trigger logic from protocol.md section 7.
        aim_err = angle (rad) between our actual torso aim and the wanted aim."""
        f = [False, False]
        size_tol = math.atan2(2.5, max(dist, 1.0))      # half-width of a mech
        for slot in (0, 1):
            w = me["weapons"][slot] if slot < len(me["weapons"]) else None
            if w is None:
                continue
            k = w["kind"]
            st = w.get("stats") or {}
            if k == "laser":
                charge = w.get("charge") or 0.0
                full = st.get("charge", 0.75)
                hold = st.get("hold_s", 2.0)
                if charge > 0:
                    if charge < full * 0.97:
                        f[slot] = True              # keep charging
                    elif aim_err > size_tol * 0.8 and charge < full + hold - 0.3:
                        f[slot] = True              # full: wait for the torso to line up
                    # else: let go -> beam fires now
                    continue
                if w.get("cooldown", 0) > 0 or dist > self._wrange(w, 280):
                    continue
                if aim_err > 1.0:
                    continue                        # torso turns while charging
                f[slot] = True                      # start a charge
                continue
            if w.get("cooldown", 0) > 0:
                continue
            if k == "gun":
                if w.get("ammo", 1) <= 0 or (w.get("reload") or 0) > 0:
                    continue
                if dist > self._wrange(w, 230) or aim_err > size_tol * 1.4 + 0.02:
                    continue
            elif k == "lightning":
                cone = math.radians(st.get("cone_deg", 30.0))
                if dist > self._wrange(w, 45) or aim_err > cone * 0.6:
                    continue
                if me.get("energy", 100) < st.get("energy", 3) + 5:
                    continue
            elif k == "mortar":
                p = self._primary(me)
                if not p or p["kind"] != "mortar":
                    continue                        # torso is aimed for another weapon's flat shot
                if dist < max(st.get("splash_radius", 7) * 1.5, 10):
                    continue                        # don't splash ourselves
                if dist > self._wrange(w, 230) or aim_err > 0.08:
                    continue
            f[slot] = True
        return f

    def _aim_error(self, me, aim):
        cur = me.get("aim") or aim
        dy = abs((cur[0] - aim[0] + math.pi) % (2 * math.pi) - math.pi)
        dp = abs(cur[1] - aim[1]) if len(cur) > 1 and len(aim) > 1 else 0.0
        return max(dy, dp)

    def _try_pickup(self, me, pickups):
        near = [p for p in pickups if self._d2(p["pos"], me["pos"]) < self.reach]
        if not near:
            return None, None
        near.sort(key=lambda p: self.weapon_prio.get(p["kind"], 0), reverse=True)
        best = near[0]
        for i, w in enumerate(me["weapons"]):
            if w is None:
                return best, None
        worst_i = min(range(len(me["weapons"])),
                      key=lambda i: self.weapon_prio.get(
                          me["weapons"][i]["kind"], 0) if me["weapons"][i] else -1)
        worst_k = me["weapons"][worst_i]["kind"] if me["weapons"][worst_i] else "gun"
        if self.weapon_prio.get(best["kind"], 0) > self.weapon_prio.get(worst_k, 0):
            return best, worst_i
        return None, None

    # =====================================================================
    # EXECUTORS
    # =====================================================================
    def _exec_zone(self, obs, act):
        me = obs["self"]
        urgent = not me["in_zone"] or self._zone_margin(obs) < 15
        dest = obs["zone"]["next_center"] if urgent else self._zone_goal(obs)
        base = self._toward(me["pos"], dest)
        act["move"] = base if urgent else self._evasive_move(obs, base)
        act["boost"] = urgent
        if urgent and me.get("grounded"):
            act["jump"] = obs["tick"] % 18 == 0

    def _exec_fight(self, obs, act):
        me = obs["self"]
        tgt = self._best_fight_target(obs)
        if not tgt:
            # hit from outside our FOV: face the attacker and close the arc
            if self.hit_bearing is not None:
                act["aim"] = [self.hit_bearing, 0.0]
                fwd = [math.cos(self.hit_bearing), math.sin(self.hit_bearing)]
                act["move"] = self._evasive_move(obs, self._perp(fwd, self.strafe_dir))
                act["boost"] = True
                mf.draw.label("TURN")
            else:
                self._exec_zone(obs, act)
            return
        d = self._d2(me["pos"], tgt["pos"])
        aim, pred = self._aim_for_fight(me, tgt)
        act["aim"] = aim
        w2 = self._w2(me)
        ideal = {"lightning": 28.0, "mortar": 60.0, "laser": 70.0}.get(w2, 45.0)
        to_t = self._toward(me["pos"], pred)
        if d > ideal + 8:
            base = to_t
        elif d < ideal - 10:
            base = [-to_t[0], -to_t[1]]
        else:
            base = self._perp(to_t, self.strafe_dir)       # circle-strafe at range
        # keep inside the zone while dueling
        margin = self._zone_margin(obs)
        if margin < 20 or not me["in_zone"]:
            zdir = self._toward(me["pos"], obs["zone"]["next_center"])
            base = self._blend(base, zdir, 0.4, 0.6)
        act["move"] = self._evasive_move(obs, base)
        act["boost"] = d > ideal + 20 and me["energy"] > 30
        act["fire"] = self._fire_slots(me, d, self._aim_error(me, aim))
        if any(act.get("fire", [])):
            mf.draw.line(me["pos"], tgt["pos"], "#ff5040")

    def _exec_flee(self, obs, act):
        """v1 flee + evasive movement + reactive jumping."""
        enemies = self._enemies(obs)
        me = obs["self"]
        if enemies:
            act["move"] = self._evasive_move(obs, self._flee_dir(obs, enemies))
            act["aim"], _ = self._aim_for_fight(me, enemies[0])
            act["fire"] = self._fire_slots(me, self._d2(me["pos"], enemies[0]["pos"]),
                                           self._aim_error(me, act["aim"]))
        else:
            goal = self._zone_goal(obs)
            act["move"] = self._evasive_move(obs, self._toward(me["pos"], goal))
        act["boost"] = True
        # reactive jump instead of fixed timer
        if self._recent_dps(obs) > 0 and me.get("grounded") and me["energy"] > 20:
            act["jump"] = True

    def _exec_cover(self, obs, act):
        """Move to cover, hold position once there, shoot back."""
        me = obs["self"]
        cover = self._get_cover(obs)
        if cover is None:
            self._exec_flee(obs, act)
            return
        d_to_cover = math.hypot(cover[0] - me["pos"][0], cover[1] - me["pos"][1])
        base = self._toward(me["pos"], cover)
        act["move"] = self._evasive_move(obs, base)
        act["boost"] = d_to_cover > 10 or self._recent_dps(obs) > 0

        enemies = self._enemies(obs)
        if enemies:
            tgt = enemies[0]
            act["aim"], _ = self._aim_for_fight(me, tgt)
            act["fire"] = self._fire_slots(me, self._d2(me["pos"], tgt["pos"]),
                                           self._aim_error(me, act["aim"]))

        if d_to_cover < 5:
            act["boost"] = False
            act["move"] = [0.0, 0.0]

        if self._recent_dps(obs) > 0 and me.get("grounded") and me["energy"] > 20:
            act["jump"] = True
        mf.draw.line(me["pos"], [cover[0], cover[1], me["pos"][2]], "#40ff40")

    def _exec_loot_weapon(self, obs, act):
        me = obs["self"]
        picks = [p for p in obs["pickups"] if p["kind"] != "gun"]
        if not picks:
            self._exec_zone(obs, act)
            return
        dest = min(picks, key=lambda p: self._d2(me["pos"], p["pos"]))
        act["move"] = self._evasive_move(obs, self._toward(me["pos"], dest["pos"]))
        act["boost"] = self._d2(me["pos"], dest["pos"]) > 35
        pickup, drop = self._try_pickup(me, obs["pickups"])
        if pickup:
            act["take"] = pickup["id"]
            if drop is not None:
                act["drop"] = drop

    def _exec_loot_cubes(self, obs, act):
        me = obs["self"]
        targets = list(obs["cubes"]) + [c for c in obs["crates"] if c.get("hp", 0) > 0]
        if not targets:
            self._exec_zone(obs, act)
            return
        dest = min(targets, key=lambda t: self._d2(me["pos"], t["pos"]))
        act["move"] = self._evasive_move(obs, self._toward(me["pos"], dest["pos"]))
        act["boost"] = self._d2(me["pos"], dest["pos"]) > 40
        if dest.get("hp", 0) > 0:
            act["aim"] = self._aim_at(me, [dest["pos"][0], dest["pos"][1], dest["pos"][2] + 0.5])
            act["fire"] = self._fire_slots(me, self._d2(me["pos"], dest["pos"]))

    def _exec_recover(self, obs, act):
        me = obs["self"]
        threat = None
        enemies = self._enemies(obs)
        if enemies:
            threat = enemies[0]["pos"]
        elif self.mem:
            threat = min(self.mem.values(), key=lambda v: self._d2(me["pos"], v[0]))[0]
        cover = self._get_cover(obs) if enemies else None
        if cover and math.hypot(cover[0] - me["pos"][0], cover[1] - me["pos"][1]) > 4:
            act["move"] = self._toward(me["pos"], cover)
            act["move"] = [act["move"][0] * 0.4, act["move"][1] * 0.4]   # < step_speed: silent
        else:
            act["move"] = [0.0, 0.0]
        if threat:
            act["aim"] = self._aim_at(me, [threat[0], threat[1], threat[2] + 4.5])
        elif self.hit_bearing is not None:
            act["aim"] = [self.hit_bearing, 0.0]
        act["fire"] = [False, False]
        mf.draw.label("REGEN")

    def _exec_drop(self, obs, act):
        me = obs["self"]
        drops = [d for d in obs.get("drops", []) if d.get("lands_in", 1) <= 3]
        if not drops:
            self._exec_zone(obs, act)
            return
        d = min(drops, key=lambda x: self._d2(me["pos"], x["pos"]))
        dist = self._d2(me["pos"], d["pos"])
        if dist < d.get("radius", 5) * 0.6:
            act["move"] = [0.0, 0.0]        # stand still inside to capture
        else:
            act["move"] = self._toward(me["pos"], d["pos"])
            act["boost"] = dist > 20
        if self.hit_bearing is not None and self._recently_hit(obs, 2.0):
            act["aim"] = [self.hit_bearing, 0.0]

    def _exec_hunt_npc(self, obs, act):
        npcs = self._npcs(obs)
        if not npcs:
            self._exec_loot_cubes(obs, act)
            return
        tgt = npcs[0]
        me = obs["self"]
        d = self._d2(me["pos"], tgt["pos"])
        aim, pred = self._aim_for_fight(me, tgt)
        base = self._toward(me["pos"], pred) if d > 25 else \
               self._n2([me["pos"][0] - tgt["pos"][0], me["pos"][1] - tgt["pos"][1]])
        act["move"] = self._evasive_move(obs, base)
        act["aim"] = aim
        act["fire"] = self._fire_slots(me, d, self._aim_error(me, aim))

    # =====================================================================
    # STATE
    # =====================================================================
    def _update_state(self, obs):
        me = obs["self"]
        tick = obs["tick"]
        now = obs["t"]
        for e in obs.get("events", []):
            t = e.get("type")
            if t == "hit" and e.get("amount", 0) > 0:
                self.last_dealt_t = now
            if t == "kill":
                self.kill_count += 1
            elif t == "damaged":
                self.dmg_ticks.append(now)
                if e.get("bearing") is not None and e.get("weapon") != "zone":
                    self.hit_bearing = e["bearing"]
                    self.hit_t = now
                    self.hit_by = e.get("by")
        # remember enemies we've seen (vision is a cone; they don't vanish when we turn)
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                self.mem[m["id"]] = (list(m["pos"]), list(m.get("vel") or [0, 0, 0]), now)
        self.mem = {k: v for k, v in self.mem.items() if now - v[2] < 5.0}
        if self.last_pos and self._d2(me["pos"], self.last_pos) < 0.1 and \
           any(abs(x) > 0.01 for x in (self._last_move or [0, 0])):
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = 0
        self.last_pos = list(me["pos"])

    # =====================================================================
    # MAIN
    # =====================================================================
    def act(self, obs: mf.Observation) -> mf.Action:
        import time
        t0 = time.perf_counter()
        act = self._act(obs)
        self._last_move = act.get("move")
        dt = (time.perf_counter() - t0) * 1000
        self.timing.append(dt)
        if dt > 30:
            mf.log(f"[slow] tick {obs['tick']} act took {dt:.1f} ms")
        if len(self.timing) >= 600:
            s = sorted(self.timing)
            mf.log(f"[timing] p50 {s[len(s)//2]:.1f} ms  p99 {s[int(len(s)*.99)]:.1f} ms  max {s[-1]:.1f} ms")
            self.timing = []
        return act

    def _act(self, obs):
        act = {"tick": obs["tick"]}
        me = obs["self"]
        self._update_state(obs)

        if obs["offer"]:
            act["pick"] = self._choose_upgrade(obs["offer"], me)

        pickup, drop = self._try_pickup(me, obs["pickups"])
        if pickup:
            act["take"] = pickup["id"]
            if drop is not None:
                act["drop"] = drop

        if self.stuck_ticks > 20:
            a = self.rng.uniform(0, 2 * math.pi)
            act.update(move=[math.cos(a), math.sin(a)], boost=True, jump=True)
            self.stuck_ticks = 0
            mf.draw.label("UNSTICK")
            return act

        scores = {
            "zone":        self._score_zone(obs),
            "fight":       self._score_fight(obs),
            "flee":        self._score_flee(obs),
            "cover":       self._score_cover(obs),
            "drop":        self._score_drop(obs),
            "recover":     self._score_recover(obs),
            "loot_weapon": self._score_loot_weapon(obs),
            "loot_cubes":  self._score_loot_cubes(obs),
            "hunt_npc":    self._score_hunt_npc(obs),
        }
        # hysteresis: stop flip-flopping between behaviors every tick
        if self.behavior in scores:
            scores[self.behavior] += 8.0
        behavior = max(scores, key=scores.get)
        self.behavior = behavior

        executors = {
            "zone":        self._exec_zone,
            "fight":       self._exec_fight,
            "flee":        self._exec_flee,
            "cover":       self._exec_cover,
            "drop":        self._exec_drop,
            "recover":     self._exec_recover,
            "loot_weapon": self._exec_loot_weapon,
            "loot_cubes":  self._exec_loot_cubes,
            "hunt_npc":    self._exec_hunt_npc,
        }
        executors[behavior](obs, act)

        if "move" not in act:
            act["move"] = [0.0, 0.0]
        move, boost = self._apply_dodge(obs, act.get("move", [0.0, 0.0]),
                                         act.get("boost", False))
        act["move"] = move
        if boost:
            act["boost"] = True
        act["move"] = self._apply_obstacle_avoidance(obs, act["move"])

        # never leave the torso pointing at nothing: visible enemy > attacker
        # bearing > remembered enemy > nearest sound
        if "aim" not in act:
            enemies = self._enemies(obs)
            if enemies:
                act["aim"], _ = self._aim_for_fight(me, enemies[0])
            elif self.hit_bearing is not None and self._recently_hit(obs, 3.0):
                act["aim"] = [self.hit_bearing, 0.0]
            elif self.mem:
                pos = min(self.mem.values(), key=lambda v: self._d2(me["pos"], v[0]))[0]
                act["aim"] = self._aim_at(me, [pos[0], pos[1], pos[2] + 4.5])
            elif obs["sounds"]:
                s = min(obs["sounds"],
                        key=lambda s: 0 if s["dist"] == "near" else
                                      1 if s["dist"] == "mid" else 2)
                act["aim"] = [s["bearing"], 0.0]

        # reload: when empty, or topping up when nobody is in sight
        for w in me["weapons"]:
            if w and w["kind"] == "gun" and not (w.get("reload") or 0):
                full = (w.get("stats") or {}).get("ammo", (w.get("stats") or {}).get("mag", 0))
                ammo = w.get("ammo", 1)
                fire = act.get("fire") or [False, False]
                if (ammo == 0 and not any(fire)) or \
                   (not self._enemies(obs) and full and ammo < full * 0.5):
                    act["reload"] = True

        margin = self._zone_margin(obs)
        top = sorted(scores.items(), key=lambda x: -x[1])[:3]
        mf.draw.label(
            f"{behavior} {top[0][1]:.0f}|{top[1][1]:.0f}|{top[2][1]:.0f} "
            f"hp{self._hp(me):.0%} z{margin:+.0f} "
            f"k{self.kill_count} a{obs['alive']}"
        )
        return act


mf.run(Bot)