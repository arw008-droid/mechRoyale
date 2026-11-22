"""v7: v6 mechanics + guerrilla tactics
  - Lightning+mortar priority, tight fire ranges (no long-range pot shots)
  - Phase-based: early=hunt isolates, late=zone+kill
  - Target isolation bonus, kill-and-relocate
  - Enemy aim: ambush unaware targets, relax flee/cover when not spotted
  - All v6 mechanical upgrades (2-iter lead, accuracy tracking, supply drops)
"""
import json
import math
import os
import random
import mechfight as mf


# =========================================================================
# ARENA MAP  (unchanged from v4)
# =========================================================================
class ArenaMap:
    CELL = 40

    def __init__(self):
        self.prisms = []
        self.grid = {}
        self.loaded = False
        self._load()

    def _load(self):
        path = "/arena/v2/world.json"
        if not os.path.exists(path):
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

                if zone_center and zone_radius:
                    if math.hypot(cx_ - zone_center[0],
                                  cy_ - zone_center[1]) > zone_radius * 0.92:
                        continue

                dist = math.hypot(cx_ - mx, cy_ - my_)
                if zone_center:
                    cover_zone_dist = math.hypot(cx_ - zone_center[0],
                                                  cy_ - zone_center[1])
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
    name = "ghost-v7"

    def __init__(self, hello: mf.Hello):
        self.config = hello["config"]
        self.reach = self.config["loot"]["pickup_radius"]
        self.rng = random.Random(hello["seed"])
        self.weapon_cfgs = self.config["weapons"]

        # v7: lightning+mortar priority
        self.weapon_prio = {"lightning": 4, "mortar": 3, "laser": 2, "gun": 1}
        self.beats = {"laser": "mortar", "mortar": "lightning", "lightning": "laser"}

        self.arena = ArenaMap()

        self.strafe_dir = 1
        self.strafe_ticks = 0
        self.strafe_flip_at = self.rng.randint(5, 12)
        self.last_pos = None
        self.stuck_ticks = 0
        self.kill_count = 0

        self.dmg_ticks = []
        self.move_phase = 0.0
        self.dodge_amplitude = self.rng.uniform(0.3, 0.6)

        self._cover_pt = None
        self._cover_tick = -99

        # v6: accuracy tracking
        self.fire_log = []
        self.hit_log = []
        self.accuracy = 0.5

        # v7: kill-and-relocate cooldown
        self.last_kill_tick = -99

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
    def _enemies(self, obs):
        me = obs["self"]
        return sorted(
            (m for m in obs["mechs"] if m["kind"] == "mech"),
            key=lambda m: self._d3(m["pos"], me["pos"]),
        )

    def _npcs(self, obs):
        me = obs["self"]
        return sorted(
            (m for m in obs["mechs"] if m["kind"] == "npc"),
            key=lambda m: self._d3(m["pos"], me["pos"]),
        )

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
        w = me["weapons"][1] if len(me["weapons"]) > 1 else None
        return w["kind"] if w else None

    def _recent_dps(self, obs):
        tick = obs["tick"]
        self.dmg_ticks = [t for t in self.dmg_ticks if tick - t < 10]
        return len(self.dmg_ticks)

    def _recent_damage_events(self, obs):
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

    # v7: check if a target is "isolated" (no allies near them)
    def _is_isolated(self, obs, target):
        """True if target has no other enemies within 30m of them."""
        count = 0
        for e in self._enemies(obs):
            if e is target:
                continue
            if self._d2(e["pos"], target["pos"]) < 30:
                count += 1
        return count == 0

    def _enemy_aim_dot(self, me, enemy):
        """+1 = enemy aim aligned toward us; negative = facing away."""
        aim = enemy.get("aim")
        if not aim:
            return 0.0
        yaw = aim[0]
        adx, ady = math.cos(yaw), math.sin(yaw)
        to_me = self._n2([me["pos"][0] - enemy["pos"][0],
                          me["pos"][1] - enemy["pos"][1]])
        return adx * to_me[0] + ady * to_me[1]

    def _is_looking_away(self, me, enemy, threshold=0.3):
        return self._enemy_aim_dot(me, enemy) < threshold

    def _is_looking_at_me(self, me, enemy, threshold=0.7):
        return self._enemy_aim_dot(me, enemy) > threshold

    def _any_enemy_aiming_at_me(self, obs, radius=45.0):
        me = obs["self"]
        for e in self._enemies(obs):
            if self._d2(me["pos"], e["pos"]) > radius:
                continue
            if self._is_looking_at_me(me, e):
                return True
        return False

    def _ambush_target(self, obs, enemy):
        me = obs["self"]
        return self._is_isolated(obs, enemy) and self._is_looking_away(me, enemy)

    # v7: game phase — early (many alive) vs late (few alive)
    def _is_late_game(self, obs):
        alive = obs.get("alive", 16)
        return alive <= 8

    # v6: accuracy tracking
    def _update_accuracy(self, obs):
        tick = obs["tick"]
        window = 50
        for e in obs.get("events", []):
            if e.get("type") == "hit":
                self.hit_log.append(tick)
        self.fire_log = [t for t in self.fire_log if tick - t < window]
        self.hit_log = [t for t in self.hit_log if tick - t < window]
        fires = len(self.fire_log)
        hits = len(self.hit_log)
        if fires >= 3:
            self.accuracy = hits / fires

    # =====================================================================
    # UPGRADE — survival-biased (unchanged)
    # =====================================================================
    def _choose_upgrade(self, offer, me):
        hp = self._hp(me)
        prios = {
            "hp": 7, "health": 7, "armor": 6, "shield": 6,
            "repair": 5, "regen": 5, "speed": 5, "move": 5,
            "energy": 4, "sensor": 3, "vision": 3,
            "damage": 3, "fire": 3, "rate": 2, "reload": 2,
        }
        if hp < 0.5:
            prios["hp"] = 10; prios["health"] = 10; prios["repair"] = 8
        best_i, best_s = 0, -1
        for i, ch in enumerate(offer["choices"]):
            txt = (ch.get("name", "") + " " + ch.get("desc", "") +
                   " " + ch.get("id", "")).lower()
            s = sum(v for k, v in prios.items() if k in txt)
            if s > best_s:
                best_s, best_i = s, i
        return best_i

    # =====================================================================
    # SCORING — v4 base + guerrilla modifiers
    # =====================================================================

    def _score_zone(self, obs):
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

    def _score_fight(self, obs):
        enemies = self._enemies(obs)
        if not enemies:
            return -50.0
        me = obs["self"]
        hp = self._hp(me)
        nearest = enemies[0]
        nearest_d = self._d2(me["pos"], nearest["pos"])
        nearby = self._nearby_count(obs, 40)
        alive = obs.get("alive", 16)
        tick = obs["tick"]

        score = hp * 30.0
        score += (1.0 - alive / 16.0) * 20.0
        if self._w2(me):
            score += 15.0
        score += min(me["cubes"], 10) * 3.0
        score -= nearby * 18.0

        if nearest_d < 15:
            score += 12.0
        elif nearest_d < 50:
            score += 6.0
        elif nearest_d > 90:
            score -= 15.0

        score += self._matchup_bonus(me, nearest)

        if hp < 0.35:
            score -= 40.0
        score -= self._recent_damage_events(obs) * 8.0

        if self._behind_cover(obs) and nearest_d < 45:
            score += 15.0

        if nearby >= 3:
            score -= 20.0

        # v6: accuracy adjustment
        if len(self.fire_log) >= 3:
            if self.accuracy < 0.15 and nearest_d > 25:
                score -= 20.0
            elif self.accuracy > 0.40:
                score += 10.0

        # === v7 guerrilla modifiers ===

        # HUGE bonus for isolated targets (the ambush moment)
        if self._is_isolated(obs, nearest):
            score += 25.0
            # extra bonus if they're close (within lightning range)
            if nearest_d < 42:
                score += 15.0
        else:
            # target has friends nearby — penalize in early game
            if not self._is_late_game(obs):
                score -= 15.0

        if self._is_looking_away(me, nearest):
            score += 18.0
            if self._is_isolated(obs, nearest):
                score += 10.0
        elif self._is_looking_at_me(me, nearest) and nearest_d < 50:
            score -= 8.0

        # kill-and-relocate: suppress fight for 15 ticks after a kill
        # (grab loot then reposition, don't get greedy)
        if not self._is_late_game(obs) and tick - self.last_kill_tick < 15:
            score -= 25.0

        # v7: don't shoot beyond 42m in early game (no pot shots)
        # In late game, full v4 ranges apply
        early_range = 55.0 if self._is_looking_away(me, nearest) else 42.0
        if not self._is_late_game(obs) and nearest_d > early_range:
            score -= 30.0

        return score

    def _score_flee(self, obs):
        me = obs["self"]
        hp = self._hp(me)
        nearby = self._nearby_count(obs, 35)
        sounds = self._sound_threat(obs)

        score = (1.0 - hp) * 50.0
        score += self._recent_damage_events(obs) * 22.0
        score += max(0, nearby - 1) * 16.0
        if me["energy"] < 25:
            score += 10.0
        score -= hp * 25.0
        if nearby == 0:
            score -= 30.0

        if sounds > 1 and nearby == 0:
            score += 15.0

        # v7: in early game, flee harder from groups (save HP for ambushes)
        if not self._is_late_game(obs) and nearby >= 2:
            score += 20.0

        if nearby > 0 and not self._any_enemy_aiming_at_me(obs, radius=45):
            score -= 12.0

        return score

    def _score_cover(self, obs):
        enemies = self._enemies(obs)
        if not enemies or not self.arena.loaded:
            return -50.0
        me = obs["self"]
        dps = self._recent_dps(obs)
        nearby = self._nearby_count(obs, 45)
        margin = self._zone_margin(obs)

        if margin < 15 or not me["in_zone"]:
            return -50.0

        if self._behind_cover(obs):
            return -10.0

        cover = self._get_cover(obs)
        if cover is None:
            return -40.0

        cover_dist = math.hypot(cover[0] - me["pos"][0], cover[1] - me["pos"][1])
        score = 0.0

        if dps > 0:
            score += 40.0 + dps * 10.0
        if nearby >= 2:
            score += 20.0 + nearby * 8.0
        elif nearby == 1:
            score += 10.0

        if cover_dist < 15:
            score += 15.0
        elif cover_dist > 40:
            score -= 10.0

        if self._hp(me) < 0.4:
            score += 20.0

        if margin < 30:
            score -= (30.0 - margin) * 1.0

        if not self._any_enemy_aiming_at_me(obs, radius=50):
            score -= 15.0

        return score

    def _score_loot_weapon(self, obs):
        me = obs["self"]
        if self._w2(me):
            # v7: still pick up better weapons even when we have 2
            # Check if any nearby pickup is higher priority than our worst
            picks = [p for p in obs["pickups"] if p["kind"] != "gun"]
            if not picks:
                return -20.0
            mep = me["pos"]
            nearby_pick = [p for p in picks if self._d2(mep, p["pos"]) < 40]
            if not nearby_pick:
                return -20.0
            best = max(nearby_pick, key=lambda p: self.weapon_prio.get(p["kind"], 0))
            # check if it's better than our worst weapon
            w0_prio = self.weapon_prio.get(
                me["weapons"][0]["kind"] if me["weapons"][0] else "gun", 1)
            w1_prio = self.weapon_prio.get(
                self._w2(me) or "gun", 1)
            worst_prio = min(w0_prio, w1_prio)
            if self.weapon_prio.get(best["kind"], 0) > worst_prio:
                best_d = self._d2(mep, best["pos"])
                score = 50.0 + self.weapon_prio.get(best["kind"], 0) * 10.0
                score -= best_d * 0.5
                if self._nearby_count(obs, 30) > 0:
                    score -= 20.0
                return score
            return -20.0

        # don't have secondary yet — same as v4 but with v7 prios
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

        # v7: after a kill, boost cube collection (grab the death drops!)
        tick = obs["tick"]
        if tick - self.last_kill_tick < 20:
            score += 20.0

        return score

    def _score_hunt_npc(self, obs):
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

    # v6: supply drop scoring
    def _score_drop(self, obs):
        drops = obs.get("drops", [])
        if not drops:
            return -60.0
        me = obs["self"]
        mep = me["pos"]
        hp = self._hp(me)
        margin = self._zone_margin(obs)

        if margin < 10 or not me["in_zone"]:
            return -50.0

        best_drop = None
        best_score = -100.0
        for drop in drops:
            d = self._d2(mep, drop["pos"])
            if d > 100:
                continue
            if drop["lands_in"] > 5.0:
                continue
            if drop["holder"] is not None and drop["holder"] != me.get("id") \
               and drop["progress"] > 0.3:
                continue

            ds = 0.0
            ds += max(0, 60 - d) * 0.8
            if me["cubes"] < self._next_up_cost(me):
                ds += 25.0
            if hp < 0.7:
                ds += (1.0 - hp) * 30.0
            if drop["lands_in"] <= 0:
                ds += 15.0
            ds += drop["progress"] * 20.0

            if ds > best_score:
                best_score = ds
                best_drop = drop

        if best_drop is None:
            return -50.0

        score = best_score
        nearby = self._nearby_count(obs, 40)
        score -= nearby * 15.0

        t = obs.get("t", 0)
        if 60 < t < 240:
            score += 10.0

        return score

    # =====================================================================
    # MOVEMENT  (unchanged from v4)
    # =====================================================================
    def _evasive_move(self, obs, base_dir):
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
        me = obs["self"]
        mx, my_ = me["pos"][0], me["pos"][1]

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
            w = 1.0 / d
            fx += (me["pos"][0] - e["pos"][0]) * w
            fy += (me["pos"][1] - e["pos"][1]) * w
        flee = self._n2([fx, fy])
        zone_dir = self._toward(me["pos"], self._zone_goal(obs))
        margin = self._zone_margin(obs)

        if margin > 25:
            cover = self._get_cover(obs)
            if cover:
                cover_dir = self._toward(me["pos"], cover)
                flee = self._blend(flee, cover_dir, 0.5, 0.5)

        if margin < 25:
            return self._blend(flee, zone_dir, 0.35, 0.65)
        return self._blend(flee, zone_dir, 0.65, 0.35)

    # =====================================================================
    # COMBAT — v6 lead + v7 tighter ranges
    # =====================================================================
    def _weapon_speed(self, me, slot=0):
        w = me["weapons"][slot] if slot < len(me["weapons"]) else None
        if w is None:
            return 80.0
        kind = w["kind"]
        if kind in ("laser", "lightning"):
            return 999.0
        return self.weapon_cfgs.get(kind, {}).get("speed", 80)

    def _lead(self, me, tgt, slot=0):
        speed = self._weapon_speed(me, slot)
        pos = tgt["pos"]
        vel = tgt["vel"]

        dist = self._d2(me["pos"], pos)
        t = dist / max(speed, 1.0)
        pred = [pos[0] + vel[0] * t,
                pos[1] + vel[1] * t,
                pos[2] + vel[2] * t * 0.5]

        dist2 = self._d2(me["pos"], pred)
        t2 = dist2 / max(speed, 1.0)
        pred2 = [pos[0] + vel[0] * t2,
                 pos[1] + vel[1] * t2,
                 pos[2] + vel[2] * t2 * 0.5]

        return pred2

    def _aim_at(self, me, pos):
        yaw = self._yaw(me["pos"], pos)
        hd = self._d2(me["pos"], pos)
        pitch = math.atan2(pos[2] - me["pos"][2] - 2.5, max(hd, 0.1))
        return [yaw, pitch]

    def _best_fight_target(self, obs):
        me = obs["self"]
        candidates = []
        for e in self._enemies(obs):
            d = self._d2(me["pos"], e["pos"])
            if d > 120:
                continue
            hp_score = 1.0 - (e["hp"] / max(e.get("max_hp", 1400), 1.0))
            iso_bonus = 15.0 if self._is_isolated(obs, e) else 0.0
            away_bonus = 12.0 if self._is_looking_away(me, e) else 0.0
            ambush_bonus = 25.0 if self._ambush_target(obs, e) else 0.0
            candidates.append(
                (hp_score * 40 - d * 0.15 + iso_bonus + away_bonus + ambush_bonus, e))
        if not candidates:
            enemies = self._enemies(obs)
            return enemies[0] if enemies else None
        return max(candidates, key=lambda x: x[0])[1]

    def _fire_slots(self, me, dist):
        """v7: tighter ranges — gun 45m, lightning 42m, mortar 8-50m."""
        f = [False, False]
        for slot in (0, 1):
            w = me["weapons"][slot] if slot < len(me["weapons"]) else None
            if w is None:
                continue
            cd = w.get("cooldown", 0)
            k = w["kind"]
            if cd > 0 and k != "laser":
                continue
            if k == "gun" and w.get("ammo", 1) <= 0:
                continue
            if k == "gun" and dist > 45:
                continue          # v7: tighter than v4's 50
            if k == "lightning" and dist > 42:
                continue
            if k == "mortar" and dist < 8:
                continue
            if k == "mortar" and dist > 55:
                continue          # v7: don't mortar beyond 55m
            f[slot] = True
        return f

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
            self._exec_zone(obs, act)
            return
        d = self._d2(me["pos"], tgt["pos"])

        fire = self._fire_slots(me, d)
        aim_slot = 0
        if not fire[0] and fire[1]:
            aim_slot = 1
        pred = self._lead(me, tgt, slot=aim_slot)
        act["aim"] = self._aim_at(me, pred)

        # v7: preferred engagement range based on weapons
        # lightning wants ~25m, mortar wants ~30-40m
        has_lightning = any(
            me["weapons"][s]["kind"] == "lightning"
            for s in range(len(me["weapons"]))
            if me["weapons"][s]
        )
        ideal = 25.0 if has_lightning else 40.0
        ambush = self._is_looking_away(me, tgt)
        if ambush:
            ideal -= 10.0

        if d > ideal + 10:
            act["move"] = self._evasive_move(obs, self._toward(me["pos"], pred))
            act["boost"] = d > 50
        elif d < ideal - 8:
            away = self._n2([me["pos"][0] - tgt["pos"][0],
                             me["pos"][1] - tgt["pos"][1]])
            act["move"] = self._evasive_move(obs, away)
        else:
            act["move"] = self._evasive_move(obs, self._toward(me["pos"], pred))
        act["fire"] = fire
        if ambush and d > ideal + 5:
            act["fire"] = [False, False]
        if any(act.get("fire", [])):
            mf.draw.line(me["pos"], tgt["pos"], "#ff5040")

    def _exec_flee(self, obs, act):
        enemies = self._enemies(obs)
        me = obs["self"]
        if enemies:
            act["move"] = self._evasive_move(obs, self._flee_dir(obs, enemies))
            act["aim"] = self._aim_at(me, enemies[0]["pos"])
            if self._d2(me["pos"], enemies[0]["pos"]) < 35:
                act["fire"] = self._fire_slots(me,
                    self._d2(me["pos"], enemies[0]["pos"]))
        else:
            goal = self._zone_goal(obs)
            act["move"] = self._evasive_move(obs, self._toward(me["pos"], goal))
        act["boost"] = True
        if self._recent_dps(obs) > 0 and me.get("grounded") and me["energy"] > 20:
            act["jump"] = True

    def _exec_cover(self, obs, act):
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
            pred = self._lead(me, tgt)
            act["aim"] = self._aim_at(me, pred)
            if self._d2(me["pos"], tgt["pos"]) < 40:
                act["fire"] = self._fire_slots(me, self._d2(me["pos"], tgt["pos"]))

        if d_to_cover < 5:
            act["boost"] = False
            act["move"] = [0.0, 0.0]

        if self._recent_dps(obs) > 0 and me.get("grounded") and me["energy"] > 20:
            act["jump"] = True
        mf.draw.line(me["pos"], [cover[0], cover[1], me["pos"][2]], "#40ff40")

    def _exec_loot_weapon(self, obs, act):
        me = obs["self"]
        # v7: find best weapon by our priority
        picks = [p for p in obs["pickups"] if p["kind"] != "gun"]
        if not picks:
            self._exec_zone(obs, act)
            return
        # sort by our priority (lightning > mortar > laser > gun)
        picks.sort(key=lambda p: self.weapon_prio.get(p["kind"], 0), reverse=True)
        dest = picks[0]
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
            act["aim"] = self._aim_at(me, dest["pos"])
            act["fire"] = self._fire_slots(me, self._d2(me["pos"], dest["pos"]))

    def _exec_hunt_npc(self, obs, act):
        npcs = self._npcs(obs)
        if not npcs:
            self._exec_loot_cubes(obs, act)
            return
        tgt = npcs[0]
        me = obs["self"]
        d = self._d2(me["pos"], tgt["pos"])
        pred = self._lead(me, tgt)
        base = self._toward(me["pos"], pred) if d > 25 else \
               self._n2([me["pos"][0] - tgt["pos"][0], me["pos"][1] - tgt["pos"][1]])
        act["move"] = self._evasive_move(obs, base)
        act["aim"] = self._aim_at(me, pred)
        act["fire"] = self._fire_slots(me, d)

    def _exec_drop(self, obs, act):
        drops = obs.get("drops", [])
        me = obs["self"]
        if not drops:
            self._exec_zone(obs, act)
            return

        best = None
        best_d = 999.0
        for drop in drops:
            if drop["lands_in"] > 3.0:
                continue
            d = self._d2(me["pos"], drop["pos"])
            if d < best_d:
                best_d = d
                best = drop

        if best is None:
            self._exec_zone(obs, act)
            return

        base = self._toward(me["pos"], best["pos"])
        act["move"] = self._evasive_move(obs, base)
        act["boost"] = best_d > 20

        if best_d < best.get("radius", 5):
            act["move"] = [0.0, 0.0]
            act["boost"] = False

        enemies = self._enemies(obs)
        if enemies:
            tgt = enemies[0]
            pred = self._lead(me, tgt)
            act["aim"] = self._aim_at(me, pred)
            ed = self._d2(me["pos"], tgt["pos"])
            if ed < 40:
                act["fire"] = self._fire_slots(me, ed)

        if self._recent_dps(obs) > 0 and me.get("grounded") and me["energy"] > 20:
            act["jump"] = True

        mf.draw.line(me["pos"], best["pos"], "#40c0ff")

    # =====================================================================
    # STATE
    # =====================================================================
    def _update_state(self, obs):
        me = obs["self"]
        tick = obs["tick"]
        for e in obs.get("events", []):
            if e.get("type") == "kill":
                self.kill_count += 1
                self.last_kill_tick = tick      # v7: track for relocate
            if e.get("type") == "damaged":
                self.dmg_ticks.append(tick)
        if self.last_pos and self._d2(me["pos"], self.last_pos) < 0.1:
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = 0
        self.last_pos = list(me["pos"])

        self._update_accuracy(obs)

    # =====================================================================
    # MAIN
    # =====================================================================
    def act(self, obs: mf.Observation) -> mf.Action:
        act: mf.Action = {"tick": obs["tick"]}
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
            "loot_weapon": self._score_loot_weapon(obs),
            "loot_cubes":  self._score_loot_cubes(obs),
            "hunt_npc":    self._score_hunt_npc(obs),
            "drop":        self._score_drop(obs),
        }
        behavior = max(scores, key=scores.get)

        executors = {
            "zone":        self._exec_zone,
            "fight":       self._exec_fight,
            "flee":        self._exec_flee,
            "cover":       self._exec_cover,
            "loot_weapon": self._exec_loot_weapon,
            "loot_cubes":  self._exec_loot_cubes,
            "hunt_npc":    self._exec_hunt_npc,
            "drop":        self._exec_drop,
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

        if "aim" not in act:
            enemies = self._enemies(obs)
            if enemies:
                act["aim"] = self._aim_at(me, enemies[0]["pos"])
            elif obs["sounds"]:
                s = min(obs["sounds"],
                        key=lambda s: 0 if s["dist"] == "near" else
                                      1 if s["dist"] == "mid" else 2)
                act["aim"] = [s["bearing"], 0.0]

        w0 = me["weapons"][0]
        if w0 and w0["kind"] == "gun" and w0.get("ammo", 1) == 0 \
           and not act.get("fire", [False])[0]:
            act["reload"] = True

        # v6: log fires for accuracy tracking
        tick = obs["tick"]
        if any(act.get("fire", [])):
            self.fire_log.append(tick)

        margin = self._zone_margin(obs)
        top = sorted(scores.items(), key=lambda x: -x[1])[:3]
        behind = "C" if self._behind_cover(obs) else "X"
        phase = "LATE" if self._is_late_game(obs) else "HUNT"
        enemies = self._enemies(obs)
        if enemies and self._ambush_target(obs, enemies[0]):
            spot = "A"
        elif enemies and self._is_looking_at_me(me, enemies[0]):
            spot = "!"
        else:
            spot = "-"
        mf.draw.label(
            f"{behavior} {top[0][1]:.0f}|{top[1][1]:.0f}|{top[2][1]:.0f} "
            f"hp{self._hp(me):.0%} z{margin:+.0f} {behind} {spot} "
            f"k{self.kill_count} a{obs['alive']} {phase} acc{self.accuracy:.0%}"
        )
        return act


mf.run(Bot)