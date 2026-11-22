"""Utility-scored survival bot v2: survive first, fight only when cornered."""
import math
import random
import mechfight as mf


class Bot:
    name = "ghost-v2"

    def __init__(self, hello: mf.Hello):
        self.config = hello["config"]
        self.reach = self.config["loot"]["pickup_radius"]
        self.rng = random.Random(hello["seed"])
        self.weapon_cfgs = self.config["weapons"]

        self.weapon_prio = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 1}

        self.strafe_dir = 1
        self.strafe_ticks = 0
        self.strafe_flip_at = self.rng.randint(5, 12)
        self.last_pos = None
        self.stuck_ticks = 0
        self.kill_count = 0

        # --- track damage history for reactive decisions ---
        self.dmg_ticks = []        # list of ticks we took damage
        self.last_damage_bearing = None
        self.hp_history = []       # rolling HP to detect burst damage

        # --- movement unpredictability ---
        self.move_phase = 0.0      # phase for sinusoidal dodge overlay
        self.dodge_amplitude = self.rng.uniform(0.3, 0.6)

    # =====================================================================
    # VECTOR MATH (unchanged)
    # =====================================================================
    def _dist2(self, a, b):
        return math.dist(a[:2], b[:2])

    def _dist3(self, a, b):
        return math.dist(a[:3], b[:3])

    def _yaw(self, src, dst):
        return math.atan2(dst[1] - src[1], dst[0] - src[0])

    def _norm2(self, v):
        L = math.hypot(v[0], v[1])
        return [v[0] / L, v[1] / L] if L > 0.001 else [0.0, 0.0]

    def _clamp2(self, v, cap=1.0):
        L = math.hypot(v[0], v[1])
        return [v[0] / L * cap, v[1] / L * cap] if L > cap else v

    def _blend(self, a, b, wa=0.5, wb=0.5):
        return self._clamp2([a[0] * wa + b[0] * wb, a[1] * wa + b[1] * wb])

    def _toward(self, src, dst):
        y = self._yaw(src, dst)
        return [math.cos(y), math.sin(y)]

    def _perp(self, v, direction=1):
        """Perpendicular vector (rotated 90 degrees)."""
        return [-v[1] * direction, v[0] * direction]

    # =====================================================================
    # QUERIES
    # =====================================================================
    def _enemies(self, obs):
        me = obs["self"]
        return sorted(
            (m for m in obs["mechs"] if m["kind"] == "mech"),
            key=lambda m: self._dist3(m["pos"], me["pos"]),
        )

    def _npcs(self, obs):
        me = obs["self"]
        return sorted(
            (m for m in obs["mechs"] if m["kind"] == "npc"),
            key=lambda m: self._dist3(m["pos"], me["pos"]),
        )

    def _zone_margin(self, obs):
        me = obs["self"]
        return obs["zone"]["radius"] - self._dist2(me["pos"], obs["zone"]["center"])

    def _hp(self, me):
        return me["hp"] / max(me["max_hp"], 1.0)

    def _nearby_count(self, obs, radius):
        me = obs["self"]
        return sum(1 for e in self._enemies(obs) if self._dist2(me["pos"], e["pos"]) < radius)

    def _next_upgrade_cost(self, me):
        t = self.config["cubes"]["upgrade_thresholds"]
        n = len(me["upgrades"])
        return t[n] if n < len(t) else float("inf")

    def _weapon2(self, me):
        w = me["weapons"][1] if len(me["weapons"]) > 1 else None
        return w["kind"] if w else None

    def _recent_dps(self, obs):
        """How many damage events in last ~1 second (10 ticks)."""
        tick = obs["tick"]
        self.dmg_ticks = [t for t in self.dmg_ticks if tick - t < 10]
        return len(self.dmg_ticks)

    def _threat_direction(self, obs):
        """Average direction enemies are coming from, or None."""
        enemies = self._enemies(obs)
        if not enemies:
            return None
        me = obs["self"]
        fx, fy = 0.0, 0.0
        for e in enemies[:5]:
            d = max(self._dist2(me["pos"], e["pos"]), 1.0)
            w = 1.0 / (d * d)   # inverse-square: close enemies matter WAY more
            fx += (e["pos"][0] - me["pos"][0]) * w
            fy += (e["pos"][1] - me["pos"][1]) * w
        L = math.hypot(fx, fy)
        if L < 0.001:
            return None
        return [fx / L, fy / L]

    def _sound_threat(self, obs):
        """Are there close sounds indicating approaching enemies we can't see?"""
        near_sounds = sum(1 for s in obs["sounds"]
                         if s["dist"] == "near" and s["kind"] in ("step", "gun", "mortar", "laser", "lightning"))
        mid_sounds = sum(1 for s in obs["sounds"]
                        if s["dist"] == "mid" and s["kind"] in ("step", "gun"))
        return near_sounds + mid_sounds * 0.5

    # =====================================================================
    # UPGRADE SELECTION
    # =====================================================================
    def _choose_upgrade(self, offer, me):
        # ALWAYS prioritize survival: hp, armor, speed, energy
        # damage only matters late game
        hp = self._hp(me)
        prios = {
            "hp": 8, "health": 8, "armor": 7, "shield": 7,
            "repair": 6, "regen": 6, "speed": 6, "move": 5,
            "energy": 5, "sensor": 3, "vision": 3,
            "damage": 2, "fire": 2, "rate": 2, "reload": 1,
        }
        if hp < 0.5:
            prios["hp"] = 12
            prios["health"] = 12
            prios["repair"] = 10

        best_i, best_s = 0, -1
        for i, ch in enumerate(offer["choices"]):
            txt = (ch.get("name", "") + " " + ch.get("desc", "") + " " + ch.get("id", "")).lower()
            s = sum(v for k, v in prios.items() if k in txt)
            if s > best_s:
                best_s, best_i = s, i
        return best_i

    # =====================================================================
    # BEHAVIOR SCORING  —  THE KEY CHANGE: SURVIVAL FIRST
    # =====================================================================

    def _score_zone(self, obs):
        me = obs["self"]
        margin = self._zone_margin(obs)
        # outside zone is an emergency, always
        if not me["in_zone"]:
            return 200.0
        if margin < 5:
            return 150.0
        if margin < 15:
            return 60.0
        if margin < 30:
            return 25.0
        # zone shrinking soon?
        t = obs.get("t", 0)
        shrink = obs["zone"].get("shrink_start", 999)
        if t > shrink - 5 and margin < 50:
            return 40.0
        return 5.0

    def _score_fight(self, obs):
        """MUCH more conservative: only fight when cornered or easy kill available."""
        enemies = self._enemies(obs)
        if not enemies:
            return -100.0

        me = obs["self"]
        hp = self._hp(me)
        nearest = enemies[0]
        d = self._dist2(me["pos"], nearest["pos"])
        nearby = self._nearby_count(obs, 40)
        alive = obs.get("alive", 16)

        # BASE: start negative — fighting is BAD in 1v15
        score = -20.0

        # only consider fighting if:
        # 1) enemy is so close we can't run (cornered)
        if d < 10:
            score += 40.0  # forced engagement

        # 2) enemy is very low HP (easy kill for cubes)
        enemy_hp = nearest.get("hp", 999)
        enemy_max = nearest.get("max_hp", 1400)
        if enemy_hp < enemy_max * 0.2 and d < 35:
            score += 50.0  # easy kill

        # penalties
        score -= nearby * 20.0           # multiple enemies = death
        if hp < 0.4:
            score -= 50.0                # low HP, don't fight
        if not self._weapon2(me):
            score -= 20.0                # no secondary, disadvantaged

        # late game with few alive: more willing to fight
        if alive <= 4:
            score += 30.0
        elif alive <= 8:
            score += 15.0

        return score

    def _score_flee(self, obs):
        """MUCH higher priority: flee is the default safe behavior."""
        me = obs["self"]
        hp = self._hp(me)
        nearby = self._nearby_count(obs, 40)
        dps = self._recent_dps(obs)
        sounds = self._sound_threat(obs)

        # base: always somewhat inclined to flee when enemies are near
        score = 0.0

        if nearby > 0:
            score += 50.0                   # enemies visible = lean toward flee
            score += (nearby - 1) * 25.0    # multiple enemies = very high flee
        if dps > 0:
            score += dps * 30.0             # actively taking damage = RUN
        if hp < 0.6:
            score += (1.0 - hp) * 60.0      # lower HP = more desperate to flee
        if hp < 0.3:
            score += 50.0                   # critical HP = maximum flee
        if sounds > 1 and nearby == 0:
            score += 20.0                   # enemies approaching, pre-evade!

        # don't flee if nobody is around
        if nearby == 0 and dps == 0 and sounds < 1:
            score = -30.0

        return score

    def _score_loot_weapon(self, obs):
        me = obs["self"]
        if self._weapon2(me):
            return -40.0  # already have secondary
        picks = [p for p in obs["pickups"] if p["kind"] != "gun"]
        if not picks:
            return -50.0

        mep = me["pos"]
        nearby_pick = [p for p in picks if self._dist2(mep, p["pos"]) < 60]
        if not nearby_pick:
            return -40.0

        best = max(nearby_pick, key=lambda p: self.weapon_prio.get(p["kind"], 0))
        d = self._dist2(mep, best["pos"])
        score = 60.0 + self.weapon_prio.get(best["kind"], 0) * 8.0
        score -= d * 0.5

        # danger penalty
        enemies_close = self._nearby_count(obs, 30)
        score -= enemies_close * 25.0

        # early game bonus: getting a weapon is critical
        if obs.get("t", 0) < 60:
            score += 25.0

        return score

    def _score_loot_cubes(self, obs):
        me = obs["self"]
        mep = me["pos"]
        targets = list(obs["cubes"]) + [c for c in obs["crates"] if c.get("hp", 0) > 0]
        if not targets:
            return -50.0

        best_d = min(self._dist2(mep, t["pos"]) for t in targets)
        need = me["cubes"] < self._next_upgrade_cost(me)
        score = (30.0 if need else 5.0) + 15.0 / max(best_d, 1.0)

        enemies_close = self._nearby_count(obs, 35)
        score -= enemies_close * 20.0

        # late game: cubes matter less
        if obs.get("t", 0) > 250:
            score *= 0.4
        return score

    def _score_hunt_npc(self, obs):
        npcs = self._npcs(obs)
        if not npcs:
            return -80.0
        me = obs["self"]
        t = obs.get("t", 0)
        if t > 150:
            return -60.0  # NPCs are bait late game
        d = self._dist2(me["pos"], npcs[0]["pos"])
        score = 25.0 * (1.0 - t / 420.0)
        if me["cubes"] < 3 and self._weapon2(me):
            score += 20.0
        score -= d * 0.4
        # CRITICAL: don't hunt if enemies nearby
        score -= self._nearby_count(obs, 50) * 30.0
        return score

    def _score_evade(self, obs):
        """NEW: proactive evasion — move unpredictably when enemies are in mid-range."""
        enemies = self._enemies(obs)
        if not enemies:
            return -20.0
        me = obs["self"]
        nearest_d = self._dist2(me["pos"], enemies[0]["pos"])
        nearby = self._nearby_count(obs, 60)

        # sweet spot: enemies visible but not on top of us
        if 15 < nearest_d < 60 and nearby > 0:
            score = 35.0 + nearby * 10.0
            if self._hp(me) > 0.5:
                score += 10.0  # healthy enough to kite, not desperate enough to flee
            return score
        return -15.0

    # =====================================================================
    # MOVEMENT — MUCH MORE UNPREDICTABLE
    # =====================================================================
    def _strafe(self, base_dir):
        self.strafe_ticks += 1
        if self.strafe_ticks >= self.strafe_flip_at:
            self.strafe_dir *= -1
            self.strafe_ticks = 0
            self.strafe_flip_at = self.rng.randint(4, 10)  # faster flips
        if base_dir is None:
            return [0.0, 0.0]
        bx, by = base_dir
        perp = [-by * self.strafe_dir, bx * self.strafe_dir]
        return self._clamp2([bx * 0.5 + perp[0] * 0.5, by * 0.5 + perp[1] * 0.5])

    def _evasive_move(self, obs, base_dir):
        """Add sinusoidal dodge overlay to make movement harder to predict."""
        self.move_phase += 0.15 + self.rng.uniform(-0.03, 0.03)
        sin_offset = math.sin(self.move_phase) * self.dodge_amplitude

        if base_dir is None:
            base_dir = [0.0, 1.0]
        perp = self._perp(base_dir, self.strafe_dir)

        # base movement + sinusoidal perpendicular weave
        result = [
            base_dir[0] * 0.6 + perp[0] * sin_offset,
            base_dir[1] * 0.6 + perp[1] * sin_offset,
        ]
        return self._clamp2(result)

    def _dodge_dir(self, obs):
        """Dodge projectiles AND laser charges aimed at us."""
        me = obs["self"]
        mx, my = me["pos"][0], me["pos"][1]

        # check projectiles
        best_threat, best_dot = None, 0.0
        for p in obs["projectiles"]:
            dx, dy = mx - p["pos"][0], my - p["pos"][1]
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
            perp = self._norm2([-best_threat["vel"][1], best_threat["vel"][0]])
            return [perp[0] * self.strafe_dir, perp[1] * self.strafe_dir]

        # check laser charges aimed at us
        for m in obs["mechs"]:
            if m["kind"] != "mech" or not m.get("charging"):
                continue
            if self._dist2(me["pos"], m["pos"]) > 60:
                continue
            aim_yaw = m["aim"][0]
            aim_dx, aim_dy = math.cos(aim_yaw), math.sin(aim_yaw)
            to_me = self._norm2([mx - m["pos"][0], my - m["pos"][1]])
            if aim_dx * to_me[0] + aim_dy * to_me[1] > 0.6:
                return self._norm2([-aim_dy, aim_dx])

        # check beams that just fired near us
        for b in obs.get("beams", []):
            if b["age"] > 0.3:
                continue
            if len(b["path"]) < 2:
                continue
            # beam came close, dodge perpendicular to beam direction
            p0, p1 = b["path"][0], b["path"][-1]
            beam_dx = p1[0] - p0[0]
            beam_dy = p1[1] - p0[1]
            # check if beam passed near us
            to_start = [mx - p0[0], my - p0[1]]
            beam_len = math.hypot(beam_dx, beam_dy)
            if beam_len < 0.1:
                continue
            # project our position onto beam line
            t_proj = (to_start[0] * beam_dx + to_start[1] * beam_dy) / (beam_len * beam_len)
            if 0 < t_proj < 1:
                closest_x = p0[0] + beam_dx * t_proj
                closest_y = p0[1] + beam_dy * t_proj
                perp_dist = math.hypot(mx - closest_x, my - closest_y)
                if perp_dist < 8:  # beam passed close
                    return self._norm2([-beam_dy * self.strafe_dir, beam_dx * self.strafe_dir])

        return None

    def _apply_dodge(self, obs, move, boost=False):
        dodge = self._dodge_dir(obs)
        margin = self._zone_margin(obs)
        if dodge:
            if margin < 10 or not obs["self"]["in_zone"]:
                zone = self._toward(obs["self"]["pos"], obs["zone"]["next_center"])
                move = self._blend(dodge, zone, 0.5, 0.5)
            else:
                move = dodge
            boost = True
        return move, boost

    def _zone_goal(self, obs):
        """Position away from enemies within the zone."""
        me = obs["self"]
        c = obs["zone"]["next_center"]
        r = obs["zone"]["next_radius"]

        # try to position on the OPPOSITE side of the zone from the threat
        threat = self._threat_direction(obs)
        if threat and r > 10:
            # move to opposite side of zone from enemies
            away = [-threat[0], -threat[1]]
            target_r = r * 0.5
            return [c[0] + away[0] * target_r, c[1] + away[1] * target_r, me["pos"][2]]

        # fallback: aim for ~55% from center, in our current direction
        dx = me["pos"][0] - c[0]
        dy = me["pos"][1] - c[1]
        d = math.hypot(dx, dy)
        if d < 1.0:
            a = self.rng.uniform(0, 2 * math.pi)
            dx, dy, d = math.cos(a), math.sin(a), 1.0
        s = r * 0.55 / d
        return [c[0] + dx * s, c[1] + dy * s, me["pos"][2]]

    def _flee_dir(self, obs, enemies):
        me = obs["self"]
        # inverse-square weighted repulsion from enemies
        fx, fy = 0.0, 0.0
        for e in enemies[:6]:
            d = max(self._dist2(me["pos"], e["pos"]), 1.0)
            w = 1.0 / (d * d)
            fx += (me["pos"][0] - e["pos"][0]) * w
            fy += (me["pos"][1] - e["pos"][1]) * w
        flee = self._norm2([fx, fy])

        # blend with zone direction based on how close to edge
        zone_dir = self._toward(me["pos"], self._zone_goal(obs))
        margin = self._zone_margin(obs)
        if margin < 15:
            return self._blend(flee, zone_dir, 0.25, 0.75)
        elif margin < 30:
            return self._blend(flee, zone_dir, 0.45, 0.55)
        return self._blend(flee, zone_dir, 0.7, 0.3)

    # =====================================================================
    # COMBAT
    # =====================================================================
    def _lead(self, me, tgt):
        dist = self._dist2(me["pos"], tgt["pos"])
        w0 = me["weapons"][0]
        kind = w0["kind"] if w0 else "gun"
        if kind in ("laser", "lightning"):
            speed = 999.0
        else:
            speed = self.weapon_cfgs.get(kind, {}).get("speed", 80)
        t = dist / max(speed, 1.0)
        return [
            tgt["pos"][0] + tgt["vel"][0] * t,
            tgt["pos"][1] + tgt["vel"][1] * t,
            tgt["pos"][2] + tgt["vel"][2] * t * 0.4,
        ]

    def _aim_at(self, me, pos):
        yaw = self._yaw(me["pos"], pos)
        hd = self._dist2(me["pos"], pos)
        pitch = math.atan2(pos[2] - me["pos"][2] - 2.5, max(hd, 0.1))
        return [yaw, pitch]

    def _fire_slots(self, me, dist):
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
            if k == "lightning" and dist > 40:
                continue
            if k == "mortar" and dist < 8:
                continue
            if k == "gun" and dist > 50:
                continue  # gun is inaccurate at long range, save ammo
            f[slot] = True
        return f

    def _try_pickup(self, me, pickups):
        near = [p for p in pickups if self._dist2(p["pos"], me["pos"]) < self.reach]
        if not near:
            return None, None
        near.sort(key=lambda p: self.weapon_prio.get(p["kind"], 0), reverse=True)
        best = near[0]
        for i, w in enumerate(me["weapons"]):
            if w is None:
                return best, None
        worst_i = min(
            range(len(me["weapons"])),
            key=lambda i: self.weapon_prio.get(me["weapons"][i]["kind"], 0) if me["weapons"][i] else -1,
        )
        worst_k = me["weapons"][worst_i]["kind"] if me["weapons"][worst_i] else "gun"
        if self.weapon_prio.get(best["kind"], 0) > self.weapon_prio.get(worst_k, 0):
            return best, worst_i
        return None, None

    # =====================================================================
    # BEHAVIOR EXECUTORS
    # =====================================================================
    def _exec_zone(self, obs, act):
        me = obs["self"]
        urgent = not me["in_zone"] or self._zone_margin(obs) < 15
        dest = obs["zone"]["next_center"] if urgent else self._zone_goal(obs)
        base = self._toward(me["pos"], dest)
        act["move"] = self._evasive_move(obs, base) if not urgent else base
        act["boost"] = urgent
        if urgent and me.get("grounded"):
            act["jump"] = obs["tick"] % 12 == 0

    def _exec_fight(self, obs, act):
        me = obs["self"]
        enemies = self._enemies(obs)
        if not enemies:
            self._exec_zone(obs, act)
            return
        # pick weakest nearby enemy (easiest kill)
        candidates = [(e, self._dist2(me["pos"], e["pos"])) for e in enemies if self._dist2(me["pos"], e["pos"]) < 60]
        if candidates:
            # prefer low HP targets
            tgt = min(candidates, key=lambda x: x[0].get("hp", 999))[0]
        else:
            tgt = enemies[0]

        d = self._dist2(me["pos"], tgt["pos"])
        pred = self._lead(me, tgt)
        act["aim"] = self._aim_at(me, pred)

        # kite: maintain distance, never rush in
        ideal = 30.0
        if d > ideal + 10:
            act["move"] = self._evasive_move(obs, self._toward(me["pos"], pred))
        elif d < ideal - 5:
            away = self._norm2([me["pos"][0] - tgt["pos"][0], me["pos"][1] - tgt["pos"][1]])
            act["move"] = self._evasive_move(obs, away)
        else:
            # circle strafe at ideal range
            to_tgt = self._norm2([tgt["pos"][0] - me["pos"][0], tgt["pos"][1] - me["pos"][1]])
            act["move"] = self._clamp2(self._perp(to_tgt, self.strafe_dir))

        act["fire"] = self._fire_slots(me, d)
        act["boost"] = d < 15  # boost to escape if too close
        if any(act.get("fire", [])):
            mf.draw.line(me["pos"], tgt["pos"], "#ff5040")

    def _exec_flee(self, obs, act):
        enemies = self._enemies(obs)
        me = obs["self"]
        if enemies:
            flee = self._flee_dir(obs, enemies)
            act["move"] = self._evasive_move(obs, flee)
            # shoot behind us while running (suppressive fire)
            if enemies and self._dist2(me["pos"], enemies[0]["pos"]) < 35:
                pred = self._lead(me, enemies[0])
                act["aim"] = self._aim_at(me, pred)
                act["fire"] = self._fire_slots(me, self._dist2(me["pos"], enemies[0]["pos"]))
        else:
            # fleeing from sounds/damage, just move erratically toward zone
            goal = self._zone_goal(obs)
            act["move"] = self._evasive_move(obs, self._toward(me["pos"], goal))

        act["boost"] = True
        # reactive jumping: jump when taking damage, not on fixed timer
        if self._recent_dps(obs) > 0 and me.get("grounded") and me["energy"] > 20:
            act["jump"] = True

    def _exec_evade(self, obs, act):
        """Proactive evasion: enemies in mid range, move unpredictably toward safety."""
        me = obs["self"]
        goal = self._zone_goal(obs)
        base = self._toward(me["pos"], goal)
        act["move"] = self._evasive_move(obs, base)
        act["boost"] = False  # conserve energy

        # aim at nearest enemy but don't fire unless they're close
        enemies = self._enemies(obs)
        if enemies:
            tgt = enemies[0]
            d = self._dist2(me["pos"], tgt["pos"])
            pred = self._lead(me, tgt)
            act["aim"] = self._aim_at(me, pred)
            if d < 35:
                act["fire"] = self._fire_slots(me, d)

        # occasional jump to throw off aim prediction
        if obs["tick"] % 25 == 0 and me.get("grounded") and me["energy"] > 30:
            act["jump"] = True

    def _exec_loot_weapon(self, obs, act):
        me = obs["self"]
        picks = [p for p in obs["pickups"] if p["kind"] != "gun"]
        if not picks:
            self._exec_zone(obs, act)
            return
        dest = max(picks, key=lambda p: self.weapon_prio.get(p["kind"], 0) / max(self._dist2(me["pos"], p["pos"]), 1))
        base = self._toward(me["pos"], dest["pos"])
        act["move"] = self._evasive_move(obs, base)
        act["boost"] = self._dist2(me["pos"], dest["pos"]) > 30
        pickup, drop_slot = self._try_pickup(me, obs["pickups"])
        if pickup:
            act["take"] = pickup["id"]
            if drop_slot is not None:
                act["drop"] = drop_slot

    def _exec_loot_cubes(self, obs, act):
        me = obs["self"]
        targets = list(obs["cubes"]) + [c for c in obs["crates"] if c.get("hp", 0) > 0]
        if not targets:
            self._exec_zone(obs, act)
            return
        dest = min(targets, key=lambda t: self._dist2(me["pos"], t["pos"]))
        base = self._toward(me["pos"], dest["pos"])
        act["move"] = self._evasive_move(obs, base)
        act["boost"] = self._dist2(me["pos"], dest["pos"]) > 35
        # shoot crates
        if dest.get("hp", 0) > 0:
            act["aim"] = self._aim_at(me, dest["pos"])
            act["fire"] = self._fire_slots(me, self._dist2(me["pos"], dest["pos"]))

    def _exec_hunt_npc(self, obs, act):
        npcs = self._npcs(obs)
        if not npcs:
            self._exec_loot_cubes(obs, act)
            return
        tgt = npcs[0]
        me = obs["self"]
        d = self._dist2(me["pos"], tgt["pos"])
        pred = self._lead(me, tgt)
        base = self._toward(me["pos"], pred) if d > 20 else self._norm2(
            [me["pos"][0] - tgt["pos"][0], me["pos"][1] - tgt["pos"][1]])
        act["move"] = self._evasive_move(obs, base)
        act["aim"] = self._aim_at(me, pred)
        act["fire"] = self._fire_slots(me, d)

    # =====================================================================
    # STATE TRACKING
    # =====================================================================
    def _update_state(self, obs):
        me = obs["self"]
        tick = obs["tick"]
        for e in obs.get("events", []):
            if e.get("type") == "kill":
                self.kill_count += 1
            if e.get("type") == "damaged":
                self.dmg_ticks.append(tick)
                self.last_damage_bearing = e.get("bearing")

        if self.last_pos and self._dist2(me["pos"], self.last_pos) < 0.1:
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = 0
        self.last_pos = list(me["pos"])

    # =====================================================================
    # MAIN TICK
    # =====================================================================
    def act(self, obs: mf.Observation) -> mf.Action:
        act: mf.Action = {"tick": obs["tick"]}
        me = obs["self"]
        self._update_state(obs)

        if obs["offer"]:
            act["pick"] = self._choose_upgrade(obs["offer"], me)

        # always pick up nearby weapons opportunistically
        pickup, drop_slot = self._try_pickup(me, obs["pickups"])
        if pickup:
            act["take"] = pickup["id"]
            if drop_slot is not None:
                act["drop"] = drop_slot

        # unstuck
        if self.stuck_ticks > 15:
            a = self.rng.uniform(0, 2 * math.pi)
            act.update(move=[math.cos(a), math.sin(a)], boost=True, jump=True)
            self.stuck_ticks = 0
            mf.draw.label("UNSTICK")
            return act

        # score all behaviors
        scores = {
            "zone":        self._score_zone(obs),
            "fight":       self._score_fight(obs),
            "flee":        self._score_flee(obs),
            "evade":       self._score_evade(obs),
            "loot_weapon": self._score_loot_weapon(obs),
            "loot_cubes":  self._score_loot_cubes(obs),
            "hunt_npc":    self._score_hunt_npc(obs),
        }
        behavior = max(scores, key=scores.get)

        executors = {
            "zone":        self._exec_zone,
            "fight":       self._exec_fight,
            "flee":        self._exec_flee,
            "evade":       self._exec_evade,
            "loot_weapon": self._exec_loot_weapon,
            "loot_cubes":  self._exec_loot_cubes,
            "hunt_npc":    self._exec_hunt_npc,
        }
        executors[behavior](obs, act)

        # apply projectile dodge overlay (overrides movement if dodging)
        if "move" not in act:
            act["move"] = [0.0, 0.0]
        move, boost = self._apply_dodge(obs, act.get("move", [0.0, 0.0]), act.get("boost", False))
        act["move"] = move
        if boost:
            act["boost"] = True

        # always face enemies if we're not aiming at something else
        if "aim" not in act:
            enemies = self._enemies(obs)
            if enemies:
                act["aim"] = self._aim_at(me, enemies[0]["pos"])
            elif obs["sounds"]:
                s = min(obs["sounds"],
                        key=lambda s: 0 if s["dist"] == "near" else 1 if s["dist"] == "mid" else 2)
                act["aim"] = [s["bearing"], 0.0]

        # auto-reload
        w0 = me["weapons"][0]
        if w0 and w0["kind"] == "gun" and w0.get("ammo", 1) == 0 and not act.get("fire", [False])[0]:
            act["reload"] = True

        # debug HUD
        margin = self._zone_margin(obs)
        top = sorted(scores.items(), key=lambda x: -x[1])[:3]
        mf.draw.label(
            f"{behavior} {top[0][1]:.0f}|{top[1][1]:.0f}|{top[2][1]:.0f} "
            f"hp{self._hp(me):.0%} z{margin:+.0f} "
            f"k{self.kill_count} a{obs['alive']}"
        )
        return act


mf.run(Bot)