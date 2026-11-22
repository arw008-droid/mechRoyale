"""DRE-style state machine bot for Mech Royale — fully implemented."""
import math
import random
import mechfight as mf


class Bot:
    name = "dre-bot"

    def __init__(self, hello: mf.Hello):
        self.config = hello["config"]
        self.reach = self.config["loot"]["pickup_radius"]
        self.slot = hello["slot"]
        self.rng = random.Random(hello["seed"])

        # weapon configs for projectile speed lookups
        self.weapon_cfgs = self.config["weapons"]

        # --- tunable thresholds ---
        self.hp_flee = 0.25
        self.hp_cautious = 0.50
        self.close_range = 15.0
        self.medium_range = 30.0
        self.engage_range = 45.0
        self.zone_panic = -5.0

        # weapon priority for pickups (higher = better)
        self.weapon_prio = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 1}

        # --- per-tick state ---
        self.strafe_dir = 1
        self.strafe_ticks = 0
        self.strafe_flip_at = self.rng.randint(8, 20)
        self.taking_damage = False
        self.last_pos = None
        self.stuck_ticks = 0

    # =====================================================================
    # VECTOR HELPERS
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

    # =====================================================================
    # QUERIES
    # =====================================================================
    def _enemies(self, obs):
        me = obs["self"]
        return sorted(
            (m for m in obs["mechs"] if m["kind"] == "mech"),
            key=lambda m: self._dist3(m["pos"], me["pos"]),
        )

    def _nearest_enemy_dist(self, obs):
        me = obs["self"]
        dists = [self._dist3(m["pos"], me["pos"])
                 for m in obs["mechs"] if m["kind"] == "mech"]
        return min(dists) if dists else float("inf")

    def _zone_margin(self, obs):
        """Positive = inside zone. Negative = outside."""
        me = obs["self"]
        return obs["zone"]["radius"] - self._dist2(me["pos"], obs["zone"]["center"])

    def _next_upgrade_cost(self, me):
        t = self.config["cubes"]["upgrade_thresholds"]
        n = len(me["upgrades"])
        return t[n] if n < len(t) else float("inf")

    # =====================================================================
    # MODE SELECTION
    # =====================================================================
    def _pick_mode(self, obs):
        me = obs["self"]
        hp = me["hp"] / me["max_hp"]
        nearest = self._nearest_enemy_dist(obs)
        margin = self._zone_margin(obs)

        if margin < self.zone_panic:
            return "zone_rush"
        if nearest < self.close_range:
            return "danger"
        if self.taking_damage and nearest < self.medium_range:
            return "danger"
        if hp < self.hp_flee and nearest < self.medium_range:
            return "danger"
        if me["weapons"][1] is None:
            return "replenish"
        if hp < self.hp_cautious and nearest > self.medium_range:
            return "replenish"
        if me["cubes"] >= self._next_upgrade_cost(me) and nearest > self.medium_range:
            return "replenish"
        return "explore"

    # =====================================================================
    # MOVEMENT
    # =====================================================================
    def _toward(self, src, dst):
        y = self._yaw(src, dst)
        return [math.cos(y), math.sin(y)]

    def _strafe(self, obs, away_from=None):
        me = obs["self"]
        self.strafe_ticks += 1
        if self.strafe_ticks >= self.strafe_flip_at:
            self.strafe_dir *= -1
            self.strafe_ticks = 0
            self.strafe_flip_at = self.rng.randint(8, 20)

        if away_from is not None:
            away = self._norm2([me["pos"][0] - away_from[0],
                                me["pos"][1] - away_from[1]])
            perp = [-away[1] * self.strafe_dir,
                      away[0] * self.strafe_dir]
            return self._blend(perp, away, 0.6, 0.4)
        else:
            yaw = me["aim"][0]
            return self._clamp2([
                -math.sin(yaw) * self.strafe_dir,
                 math.cos(yaw) * self.strafe_dir,
            ])

    def _zone_edge_goal(self, obs):
        """A point ~55% from zone center to edge, in our current direction."""
        me = obs["self"]
        c = obs["zone"]["next_center"]
        r = obs["zone"]["next_radius"]
        dx = me["pos"][0] - c[0]
        dy = me["pos"][1] - c[1]
        d = math.hypot(dx, dy)
        if d < 1.0:
            a = self.rng.uniform(0, 2 * math.pi)
            dx, dy, d = math.cos(a), math.sin(a), 1.0
        s = r * 0.55 / d
        return [c[0] + dx * s, c[1] + dy * s, me["pos"][2]]

    # =====================================================================
    # AIM & FIRE
    # =====================================================================
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

    def _fire_decision(self, me, dist):
        f = [False, False]
        for slot in (0, 1):
            w = me["weapons"][slot] if slot < len(me["weapons"]) else None
            if w is None:
                continue
            cd = w.get("cooldown", 0)
            k = w["kind"]
            if cd > 0 and k != "laser":
                continue
            if k == "gun":
                if w.get("ammo", 1) > 0 and dist < self.engage_range:
                    f[slot] = True
            elif k == "laser":
                if dist < self.engage_range:
                    f[slot] = True
            elif k == "mortar":
                if 10 < dist < self.engage_range:
                    f[slot] = True
            elif k == "lightning":
                if dist < 25:
                    f[slot] = True
        return f

    # =====================================================================
    # PROJECTILE DODGE
    # =====================================================================
    def _dodge_dir(self, obs):
        me = obs["self"]
        mx, my = me["pos"][0], me["pos"][1]
        for p in obs["projectiles"]:
            dx, dy = mx - p["pos"][0], my - p["pos"][1]
            d = math.hypot(dx, dy)
            if d > 30 or d < 1:
                continue
            vl = math.hypot(p["vel"][0], p["vel"][1])
            if vl < 0.1:
                continue
            dot = (dx * p["vel"][0] + dy * p["vel"][1]) / (d * vl)
            if dot > 0.6:
                perp = self._norm2([-p["vel"][1], p["vel"][0]])
                return [perp[0] * self.strafe_dir, perp[1] * self.strafe_dir]
        return None

    # =====================================================================
    # LOOT
    # =====================================================================
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

    def _choose_upgrade(self, offer, me):
        prios = {"hp": 5, "health": 5, "armor": 5, "shield": 4, "repair": 4,
                 "regen": 4, "speed": 3, "move": 3, "energy": 3,
                 "damage": 2, "fire": 2, "rate": 2, "reload": 2,
                 "ammo": 1, "charge": 1, "range": 1}
        best_i, best_s = 0, -1
        for i, ch in enumerate(offer["choices"]):
            txt = (ch.get("name", "") + " " + ch.get("desc", "")).lower()
            s = sum(v for k, v in prios.items() if k in txt)
            if s > best_s:
                best_s, best_i = s, i
        return best_i

    def _best_loot_target(self, obs):
        me = obs["self"]
        targets = []
        for c in obs["crates"]:
            targets.append((c["pos"], self._dist2(me["pos"], c["pos"]), 1.5))
        for c in obs["cubes"]:
            targets.append((c["pos"], self._dist2(me["pos"], c["pos"]), 2.0))
        if me["weapons"][1] is None:
            for p in obs["pickups"]:
                w = self.weapon_prio.get(p["kind"], 1)
                targets.append((p["pos"], self._dist2(me["pos"], p["pos"]), w))
        for d in obs["drops"]:
            if d["lands_in"] <= 0:
                targets.append((d["pos"], self._dist2(me["pos"], d["pos"]), 4.0))
        if not targets:
            return None
        targets.sort(key=lambda t: t[2] / max(t[1], 1.0), reverse=True)
        return targets[0][0]

    # =====================================================================
    # MODE BEHAVIORS
    # =====================================================================
    def _do_danger(self, obs, act):
        me = obs["self"]
        enemies = self._enemies(obs)

        dodge = self._dodge_dir(obs)
        if dodge:
            act["move"] = self._clamp2(dodge)
        elif enemies:
            act["move"] = self._strafe(obs, away_from=enemies[0]["pos"])
        else:
            act["move"] = self._strafe(obs)

        act["boost"] = True
        act["jump"] = obs["tick"] % 15 == 0

        if enemies:
            pred = self._lead(me, enemies[0])
            act["aim"] = self._aim_at(me, pred)
            act["fire"] = self._fire_decision(me, self._dist2(me["pos"], enemies[0]["pos"]))
            mf.draw.line(me["pos"], enemies[0]["pos"], "#ff3030")

        if not me["in_zone"]:
            z_move = self._toward(me["pos"], obs["zone"]["next_center"])
            cur = act.get("move", [0, 0])
            act["move"] = self._blend(cur, z_move, 0.55, 0.45)

        return act

    def _do_zone_rush(self, obs, act):
        me = obs["self"]
        act["move"] = self._toward(me["pos"], obs["zone"]["next_center"])
        act["boost"] = True
        enemies = self._enemies(obs)
        if enemies and self._dist2(me["pos"], enemies[0]["pos"]) < self.close_range:
            pred = self._lead(me, enemies[0])
            act["aim"] = self._aim_at(me, pred)
            act["fire"] = self._fire_decision(me, self._dist2(me["pos"], enemies[0]["pos"]))
        return act

    def _do_replenish(self, obs, act):
        me = obs["self"]
        pickup, drop_slot = self._want_pickup(me, obs["pickups"])
        if pickup:
            act["take"] = pickup["id"]
            if drop_slot is not None:
                act["drop"] = drop_slot

        loot_pos = self._best_loot_target(obs)
        if loot_pos:
            act["move"] = self._toward(me["pos"], loot_pos)
            mf.draw.point(loot_pos, "#40ff40", 6)
        else:
            act["move"] = self._toward(me["pos"], self._zone_edge_goal(obs))

        act["boost"] = not me["in_zone"]

        enemies = self._enemies(obs)
        if enemies and self._dist2(me["pos"], enemies[0]["pos"]) < self.close_range:
            pred = self._lead(me, enemies[0])
            act["aim"] = self._aim_at(me, pred)
            act["fire"] = self._fire_decision(me, self._dist2(me["pos"], enemies[0]["pos"]))
        return act

    def _do_explore(self, obs, act):
        me = obs["self"]
        enemies = self._enemies(obs)

        goal = self._zone_edge_goal(obs)
        base = self._toward(me["pos"], goal)
        if enemies:
            act["move"] = self._blend(base, self._strafe(obs), 0.5, 0.5)
        else:
            act["move"] = base

        act["boost"] = not me["in_zone"]
        act["jump"] = obs["tick"] % 30 == 0

        if enemies:
            tgt = enemies[0]
            d = self._dist2(me["pos"], tgt["pos"])
            if d < self.engage_range:
                pred = self._lead(me, tgt)
                act["aim"] = self._aim_at(me, pred)
                act["fire"] = self._fire_decision(me, d)
                mf.draw.line(me["pos"], tgt["pos"], "#ffaa00")
        elif obs["sounds"]:
            s = min(obs["sounds"],
                    key=lambda s: 0 if s["dist"] == "near" else 1 if s["dist"] == "mid" else 2)
            act["aim"] = [s["bearing"], 0.0]

        return act

    # =====================================================================
    # STATE TRACKING
    # =====================================================================
    def _update_state(self, obs):
        me = obs["self"]
        self.taking_damage = any(e.get("type") == "damaged" for e in obs["events"])
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

        # unstuck
        if self.stuck_ticks > 20:
            a = self.rng.uniform(0, 2 * math.pi)
            act.update(move=[math.cos(a), math.sin(a)], boost=True, jump=True)
            self.stuck_ticks = 0
            mf.draw.label("UNSTICKING")
            return act

        mode = self._pick_mode(obs)
        act = {"danger": self._do_danger, "zone_rush": self._do_zone_rush,
               "replenish": self._do_replenish, "explore": self._do_explore}[mode](obs, act)

        # auto-reload gun when not firing
        w0 = me["weapons"][0]
        if w0 and w0["kind"] == "gun" and w0.get("ammo", 1) == 0 \
                and not act.get("fire", [False])[0]:
            act["reload"] = True

        hp = me["hp"] / me["max_hp"]
        vis = len([m for m in obs["mechs"] if m["kind"] == "mech"])
        zm = self._zone_margin(obs)
        mf.draw.label(
            f"{mode.upper()} | hp {hp:.0%} | {vis} vis | zone {zm:+.0f}m | {obs['alive']} alive")

        return act


mf.run(Bot)