"""Pacifist Survivor bot — stay in zone, evade everything, only fight when cornered."""
import math
import random
import mechfight as mf


class Bot:
    name = "ghost"

    def __init__(self, hello: mf.Hello):
        self.config = hello["config"]
        self.reach = self.config["loot"]["pickup_radius"]
        self.slot = hello["slot"]
        self.rng = random.Random(hello["seed"])
        self.weapon_cfgs = self.config["weapons"]

        # tunable
        self.cornered_range = 8.0       # only shoot if enemy this close
        self.flee_range = 50.0           # start fleeing when enemy within this
        self.zone_target_pct = 0.55      # stay at ~55% of zone radius from center
        self.zone_panic_margin = 5.0     # rush to zone if margin < this

        self.weapon_prio = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 1}

        # state
        self.strafe_dir = 1
        self.strafe_ticks = 0
        self.strafe_flip_at = self.rng.randint(6, 14)
        self.last_pos = None
        self.stuck_ticks = 0
        self.flee_angle_offset = self.rng.choice([-1, 1]) * self.rng.uniform(0.3, 0.8)

    # === Vector helpers ===
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

    # === Queries ===
    def _enemies(self, obs):
        me = obs["self"]
        return sorted(
            (m for m in obs["mechs"] if m["kind"] == "mech"),
            key=lambda m: self._dist3(m["pos"], me["pos"]),
        )

    def _zone_margin(self, obs):
        me = obs["self"]
        return obs["zone"]["radius"] - self._dist2(me["pos"], obs["zone"]["center"])

    def _next_upgrade_cost(self, me):
        t = self.config["cubes"]["upgrade_thresholds"]
        n = len(me["upgrades"])
        return t[n] if n < len(t) else float("inf")

    # === Upgrade selection (prioritize survivability) ===
    def _choose_upgrade(self, offer, me):
        prios = {"hp": 6, "health": 6, "armor": 6, "shield": 5, "repair": 5,
                 "regen": 5, "speed": 4, "move": 4, "energy": 4, "sensor": 3,
                 "vision": 3, "damage": 1, "fire": 1, "rate": 1, "reload": 1,
                 "ammo": 1, "charge": 1, "range": 1}
        best_i, best_s = 0, -1
        for i, ch in enumerate(offer["choices"]):
            txt = (ch.get("name", "") + " " + ch.get("desc", "")).lower()
            s = sum(v for k, v in prios.items() if k in txt)
            if s > best_s:
                best_s, best_i = s, i
        return best_i

    # === Strafe (constant evasive movement) ===
    def _strafe(self, base_dir=None):
        self.strafe_ticks += 1
        if self.strafe_ticks >= self.strafe_flip_at:
            self.strafe_dir *= -1
            self.strafe_ticks = 0
            self.strafe_flip_at = self.rng.randint(6, 14)

        if base_dir is not None:
            bx, by = base_dir
            perp = [-by * self.strafe_dir, bx * self.strafe_dir]
            return self._clamp2([bx * 0.5 + perp[0] * 0.5,
                                  by * 0.5 + perp[1] * 0.5])
        return [0.0, 0.0]

    # === Projectile dodge (highest priority movement override) ===
    def _dodge_dir(self, obs):
        me = obs["self"]
        mx, my = me["pos"][0], me["pos"][1]
        best_threat = None
        best_dot = 0
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
                best_dot = dot
                best_threat = p
        if best_threat is not None:
            perp = self._norm2([-best_threat["vel"][1], best_threat["vel"][0]])
            return [perp[0] * self.strafe_dir, perp[1] * self.strafe_dir]

        # Also dodge beams/lasers being charged at us
        for m in obs["mechs"]:
            if m["kind"] != "mech":
                continue
            if not m.get("charging"):
                continue
            d = self._dist2(me["pos"], m["pos"])
            if d < 60:
                aim_yaw = m["aim"][0]
                aim_dx = math.cos(aim_yaw)
                aim_dy = math.sin(aim_yaw)
                to_me = self._norm2([mx - m["pos"][0], my - m["pos"][1]])
                dot = aim_dx * to_me[0] + aim_dy * to_me[1]
                if dot > 0.7:
                    perp = self._norm2([-aim_dy, aim_dx])
                    return [perp[0] * self.strafe_dir, perp[1] * self.strafe_dir]

        return None

    # === Zone-safe goal point ===
    def _zone_goal(self, obs):
        me = obs["self"]
        c = obs["zone"]["next_center"]
        r = obs["zone"]["next_radius"]
        dx = me["pos"][0] - c[0]
        dy = me["pos"][1] - c[1]
        d = math.hypot(dx, dy)
        if d < 1.0:
            a = self.rng.uniform(0, 2 * math.pi)
            dx, dy, d = math.cos(a), math.sin(a), 1.0
        s = r * self.zone_target_pct / d
        return [c[0] + dx * s, c[1] + dy * s, me["pos"][2]]

    # === Flee direction (away from enemies, biased toward zone center) ===
    def _flee_dir(self, obs, enemies):
        me = obs["self"]
        # Sum up threat vectors (inverse-distance weighted)
        fx, fy = 0.0, 0.0
        for e in enemies[:4]:
            d = max(self._dist2(me["pos"], e["pos"]), 1.0)
            w = 1.0 / d
            fx += (me["pos"][0] - e["pos"][0]) * w
            fy += (me["pos"][1] - e["pos"][1]) * w

        flee = self._norm2([fx, fy])

        # Blend with zone-safe direction to never flee out of zone
        zone_goal = self._zone_goal(obs)
        zone_dir = self._toward(me["pos"], zone_goal)
        margin = self._zone_margin(obs)

        if margin < 20:
            # Strongly bias toward zone
            return self._blend(flee, zone_dir, 0.3, 0.7)
        else:
            return self._blend(flee, zone_dir, 0.6, 0.4)

    # === Loot (opportunistic, only grab things nearby on our path) ===
    def _want_pickup(self, me, pickups):
        near = [p for p in pickups
                if self._dist2(p["pos"], me["pos"]) < self.reach]
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

    # === Nearby loot worth grabbing while fleeing ===
    def _nearby_loot(self, obs):
        me = obs["self"]
        best, best_score = None, -1
        for c in obs["cubes"]:
            d = self._dist2(me["pos"], c["pos"])
            if d < 12:
                s = 2.0 / max(d, 1.0)
                if s > best_score:
                    best_score, best = s, c["pos"]
        for c in obs["crates"]:
            d = self._dist2(me["pos"], c["pos"])
            if d < 8:
                s = 1.0 / max(d, 1.0)
                if s > best_score:
                    best_score, best = s, c["pos"]
        if me["weapons"][1] is None:
            for p in obs["pickups"]:
                d = self._dist2(me["pos"], p["pos"])
                if d < 10:
                    s = self.weapon_prio.get(p["kind"], 1) / max(d, 1.0)
                    if s > best_score:
                        best_score, best = s, p["pos"]
        return best

    # === Aim and fire (only when cornered) ===
    def _lead(self, me, tgt):
        dist = self._dist2(me["pos"], tgt["pos"])
        w0 = me["weapons"][0]
        if w0 is not None:
            kind = w0["kind"]
            if kind in ("laser", "lightning"):
                speed = 999
            else:
                speed = self.weapon_cfgs.get(kind, {}).get("speed", 80)
        else:
            speed = 80
        t = dist / max(speed, 1)
        return [
            tgt["pos"][0] + tgt["vel"][0] * t,
            tgt["pos"][1] + tgt["vel"][1] * t,
            tgt["pos"][2] + tgt["vel"][2] * t * 0.5,
        ]

    def _aim_at(self, me, pos):
        yaw = self._yaw(me["pos"], pos)
        hd = self._dist2(me["pos"], pos)
        pitch = math.atan2(pos[2] - me["pos"][2] - 2.5, max(hd, 0.1))
        return [yaw, pitch]

    # === State tracking ===
    def _update_state(self, obs):
        me = obs["self"]
        if self.last_pos and self._dist2(me["pos"], self.last_pos) < 0.1:
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = 0
        self.last_pos = list(me["pos"])

    # === Main tick ===
    def act(self, obs: mf.Observation) -> mf.Action:
        act: mf.Action = {"tick": obs["tick"]}
        me = obs["self"]

        self._update_state(obs)

        # Always pick upgrades (survivability priority)
        if obs["offer"]:
            act["pick"] = self._choose_upgrade(obs["offer"], me)

        # Always try to grab nearby pickups
        pickup, drop_slot = self._want_pickup(me, obs["pickups"])
        if pickup:
            act["take"] = pickup["id"]
            if drop_slot is not None:
                act["drop"] = drop_slot

        # Unstuck
        if self.stuck_ticks > 20:
            a = self.rng.uniform(0, 2 * math.pi)
            act.update(move=[math.cos(a), math.sin(a)], boost=True, jump=True)
            self.stuck_ticks = 0
            mf.draw.label("UNSTICKING")
            return act

        enemies = self._enemies(obs)
        margin = self._zone_margin(obs)
        nearest_dist = self._dist2(me["pos"], enemies[0]["pos"]) if enemies else float("inf")

        # --- Priority 1: Dodge incoming projectiles ---
        dodge = self._dodge_dir(obs)

        # --- Priority 2: Stay in zone ---
        zone_urgent = margin < self.zone_panic_margin or not me["in_zone"]

        # --- Priority 3: Flee from enemies ---
        enemies_close = nearest_dist < self.flee_range and len(enemies) > 0

        # --- Decide movement ---
        if dodge:
            # Projectile dodge overrides everything except zone panic
            if zone_urgent:
                zone_dir = self._toward(me["pos"], obs["zone"]["next_center"])
                act["move"] = self._blend(dodge, zone_dir, 0.5, 0.5)
            else:
                act["move"] = dodge
            act["boost"] = True
            act["jump"] = True
        elif zone_urgent:
            # Rush to zone center
            act["move"] = self._toward(me["pos"], obs["zone"]["next_center"])
            act["boost"] = True
        elif enemies_close:
            # Flee from enemies while staying in zone
            flee = self._flee_dir(obs, enemies)
            act["move"] = self._strafe(flee)
            act["boost"] = nearest_dist < 25
        else:
            # Safe — move toward zone sweet spot, grab loot on the way
            loot_pos = self._nearby_loot(obs)
            if loot_pos:
                base = self._toward(me["pos"], loot_pos)
            else:
                base = self._toward(me["pos"], self._zone_goal(obs))
            act["move"] = self._strafe(base)
            act["boost"] = False

        # Periodic jumping for evasion when enemies nearby
        if enemies_close and obs["tick"] % 12 == 0:
            act["jump"] = True

        # --- Aim: always face away from threats (or toward zone goal) ---
        if enemies:
            # Look at nearest enemy so we can dodge better (and show front armor)
            act["aim"] = self._aim_at(me, enemies[0]["pos"])
        elif obs["sounds"]:
            s = min(obs["sounds"],
                    key=lambda s: 0 if s["dist"] == "near" else 1 if s["dist"] == "mid" else 2)
            act["aim"] = [s["bearing"], 0.0]

        # --- Fire: ONLY when cornered (enemy very close, can't escape) ---
        if enemies and nearest_dist < self.cornered_range:
            tgt = enemies[0]
            pred = self._lead(me, tgt)
            act["aim"] = self._aim_at(me, pred)
            f = [False, False]
            for slot in (0, 1):
                w = me["weapons"][slot] if slot < len(me["weapons"]) else None
                if w is None:
                    continue
                cd = w.get("cooldown", 0)
                k = w["kind"]
                if cd > 0 and k != "laser":
                    continue
                f[slot] = True
            act["fire"] = f
            mf.draw.line(me["pos"], tgt["pos"], "#ff3030")

        # Auto-reload when not firing
        w0 = me["weapons"][0]
        if w0 and w0["kind"] == "gun" and w0.get("ammo", 1) == 0 \
                and not act.get("fire", [False])[0]:
            act["reload"] = True

        # Debug HUD
        hp = me["hp"] / me["max_hp"]
        vis = len([m for m in obs["mechs"] if m["kind"] == "mech"])
        mode = "DODGE" if dodge else "ZONE!" if zone_urgent else \
               "FLEE" if enemies_close else "ROAM"
        mf.draw.label(
            f"{mode} | hp {hp:.0%} | {vis} vis | zone {margin:+.0f}m | {obs['alive']} alive")

        return act


mf.run(Bot)
