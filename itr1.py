"""Utility-scored combat bot: fight, flee, loot, zone, NPC farm, by game state."""
import math
import random
import mechfight as mf


class Bot:
    name = "ghost"

    def __init__(self, hello: mf.Hello):
        self.config = hello["config"]
        self.reach = self.config["loot"]["pickup_radius"]
        self.rng = random.Random(hello["seed"])
        self.weapon_cfgs = self.config["weapons"]
        self.zone_target_pct = 0.6

        self.weapon_prio = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 1}
        self.beats = {"laser": "mortar", "mortar": "lightning", "lightning": "laser"}

        self.strafe_dir = 1
        self.strafe_ticks = 0
        self.strafe_flip_at = self.rng.randint(6, 14)
        self.last_pos = None
        self.stuck_ticks = 0
        self.current_behavior = "init"
        self.kill_count = 0

    # --- vector math ---
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

    # --- queries ---
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

    def _hp_ratio(self, me):
        return me["hp"] / max(me["max_hp"], 1.0)

    def _nearby_enemies(self, obs, radius):
        me = obs["self"]
        return sum(1 for e in self._enemies(obs) if self._dist2(me["pos"], e["pos"]) < radius)

    def _next_upgrade_threshold(self, me):
        t = self.config["cubes"]["upgrade_thresholds"]
        n = len(me["upgrades"])
        return t[n] if n < len(t) else float("inf")

    def _secondary_kind(self, me):
        w = me["weapons"][1] if len(me["weapons"]) > 1 else None
        return w["kind"] if w else None

    def _matchup_bonus(self, me, enemy):
        my2 = self._secondary_kind(me) or "gun"
        their = enemy.get("weapons") or []
        their2 = next((k for k in their if k != "gun"), their[0] if their else "gun")
        if self.beats.get(my2) == their2:
            return 12.0
        if self.beats.get(their2) == my2:
            return -8.0
        return 0.0

    def _recent_damage(self, obs, ticks=10):
        return sum(1 for e in obs.get("events", []) if e.get("type") == "damaged")

    def _choose_upgrade(self, offer, me):
        cubes = me["cubes"]
        phase = "early" if cubes < 6 else "mid" if cubes < 15 else "late"
        weights = {
            "early": {"hp": 6, "health": 6, "armor": 6, "shield": 5, "speed": 4, "sensor": 3, "damage": 2},
            "mid": {"armor": 5, "damage": 5, "fire": 4, "rate": 4, "sensor": 4, "hp": 3, "speed": 3},
            "late": {"damage": 6, "fire": 5, "armor": 5, "hp": 4, "rate": 4},
        }
        prios = weights[phase]
        hold = self._secondary_kind(me) or ""
        best_i, best_s = 0, -1
        for i, ch in enumerate(offer["choices"]):
            txt = (ch.get("name", "") + " " + ch.get("desc", "") + " " + ch.get("id", "")).lower()
            s = sum(v for k, v in prios.items() if k in txt)
            if hold and hold in txt:
                s += 4
            if s > best_s:
                best_s, best_i = s, i
        return best_i

    # --- scoring ---
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
        hp = self._hp_ratio(me)
        nearest_d = self._dist2(me["pos"], enemies[0]["pos"])
        nearby = self._nearby_enemies(obs, 40)
        alive = obs.get("alive", 16)

        score = hp * 30.0
        score += (1.0 - alive / 16.0) * 20.0
        if self._secondary_kind(me):
            score += 15.0
        score += min(me["cubes"], 10) * 3.0
        score -= nearby * 14.0
        if nearest_d < 15:
            score += 12.0
        elif nearest_d < 50:
            score += 6.0
        elif nearest_d > 90:
            score -= 15.0
        score += self._matchup_bonus(me, enemies[0])
        if hp < 0.35:
            score -= 40.0
        score -= self._recent_damage(obs) * 8.0
        return score

    def _score_flee(self, obs):
        me = obs["self"]
        hp = self._hp_ratio(me)
        nearby = self._nearby_enemies(obs, 35)
        score = (1.0 - hp) * 50.0
        score += self._recent_damage(obs) * 18.0
        score += max(0, nearby - 1) * 16.0
        if me["energy"] < 25:
            score += 10.0
        score -= hp * 25.0
        if nearby == 0:
            score -= 30.0
        return score

    def _score_loot_weapon(self, obs):
        me = obs["self"]
        if self._secondary_kind(me):
            return -20.0
        if not obs["pickups"]:
            return -30.0
        mep = me["pos"]
        best_d = min(self._dist2(mep, p["pos"]) for p in obs["pickups"] if p["kind"] != "gun")
        nearby_pick = [p for p in obs["pickups"] if p["kind"] != "gun" and self._dist2(mep, p["pos"]) < 80]
        if not nearby_pick:
            return -25.0
        best = max(nearby_pick, key=lambda p: self.weapon_prio.get(p["kind"], 0))
        score = 70.0 + self.weapon_prio.get(best["kind"], 0) * 10.0
        score -= best_d * 0.4
        if self._nearby_enemies(obs, 30) == 0:
            score += 18.0
        else:
            score -= self._nearby_enemies(obs, 30) * 12.0
        if obs.get("t", 0) < 90:
            score += 15.0
        return score

    def _score_loot_cubes(self, obs):
        me = obs["self"]
        need = me["cubes"] < self._next_upgrade_threshold(me)
        mep = me["pos"]
        targets = list(obs["cubes"]) + [c for c in obs["crates"] if c["hp"] > 0]
        if not targets:
            return -40.0
        best_d = min(self._dist2(mep, t["pos"]) for t in targets)
        score = (25.0 if need else 8.0) + 20.0 / max(best_d, 1.0)
        score -= self._nearby_enemies(obs, 35) * 10.0
        if obs.get("t", 0) > 200:
            score *= 0.6
        return score

    def _score_hunt_npc(self, obs):
        npcs = self._npcs(obs)
        if not npcs:
            return -60.0
        me = obs["self"]
        t = obs.get("t", 0)
        if t > 180:
            return -40.0
        d = self._dist2(me["pos"], npcs[0]["pos"])
        score = 35.0 * (1.0 - t / 420.0)
        if me["cubes"] < 3:
            score += 25.0
        score -= d * 0.35
        score -= self._nearby_enemies(obs, 45) * 18.0
        return score

    def _score_highground(self, obs):
        me = obs["self"]
        z = me["pos"][2]
        if z > 6.0:
            return -15.0
        if me["energy"] < 40:
            return -25.0
        nearby = self._nearby_enemies(obs, 80)
        if nearby == 0:
            return -20.0
        score = 12.0 + nearby * 6.0
        if self._secondary_kind(me) == "mortar":
            score += 12.0
        if not me.get("grounded", True):
            return -10.0
        return score

    # --- movement overlays ---
    def _strafe(self, base_dir):
        self.strafe_ticks += 1
        if self.strafe_ticks >= self.strafe_flip_at:
            self.strafe_dir *= -1
            self.strafe_ticks = 0
            self.strafe_flip_at = self.rng.randint(6, 14)
        if base_dir is None:
            return [0.0, 0.0]
        bx, by = base_dir
        perp = [-by * self.strafe_dir, bx * self.strafe_dir]
        return self._clamp2([bx * 0.55 + perp[0] * 0.45, by * 0.55 + perp[1] * 0.45])

    def _dodge_dir(self, obs):
        me = obs["self"]
        mx, my = me["pos"][0], me["pos"][1]
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
        for m in obs["mechs"]:
            if m["kind"] != "mech" or not m.get("charging"):
                continue
            if self._dist2(me["pos"], m["pos"]) > 60:
                continue
            aim_yaw = m["aim"][0]
            aim_dx, aim_dy = math.cos(aim_yaw), math.sin(aim_yaw)
            to_me = self._norm2([mx - m["pos"][0], my - m["pos"][1]])
            if aim_dx * to_me[0] + aim_dy * to_me[1] > 0.7:
                return self._norm2([-aim_dy, aim_dx])
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

    def _flee_dir(self, obs, enemies):
        me = obs["self"]
        fx, fy = 0.0, 0.0
        for e in enemies[:4]:
            d = max(self._dist2(me["pos"], e["pos"]), 1.0)
            w = 1.0 / d
            fx += (me["pos"][0] - e["pos"][0]) * w
            fy += (me["pos"][1] - e["pos"][1]) * w
        flee = self._norm2([fx, fy])
        zone_dir = self._toward(me["pos"], self._zone_goal(obs))
        margin = self._zone_margin(obs)
        if margin < 25:
            return self._blend(flee, zone_dir, 0.35, 0.65)
        return self._blend(flee, zone_dir, 0.65, 0.35)

    # --- combat ---
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
            tgt["pos"][2] + tgt["vel"][2] * t * 0.5,
        ]

    def _aim_at(self, me, pos):
        yaw = self._yaw(me["pos"], pos)
        hd = self._dist2(me["pos"], pos)
        pitch = math.atan2(pos[2] - me["pos"][2] - 2.5, max(hd, 0.1))
        return [yaw, pitch]

    def _best_fight_target(self, obs):
        me = obs["self"]
        candidates = []
        for e in self._enemies(obs):
            d = self._dist2(me["pos"], e["pos"])
            if d > 120:
                continue
            hp_score = 1.0 - (e["hp"] / max(e.get("max_hp", 1400), 1.0))
            candidates.append((hp_score * 40 - d * 0.15, e))
        if not candidates:
            enemies = self._enemies(obs)
            return enemies[0] if enemies else None
        return max(candidates, key=lambda x: x[0])[1]

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
            if k == "lightning" and dist > 42:
                continue
            if k == "mortar" and dist < 8:
                continue
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

    # --- behavior executors ---
    def _exec_zone(self, obs, act):
        me = obs["self"]
        urgent = not me["in_zone"] or self._zone_margin(obs) < 15
        dest = obs["zone"]["next_center"] if urgent else self._zone_goal(obs)
        act["move"] = self._toward(me["pos"], dest)
        act["boost"] = urgent
        if urgent and me.get("grounded"):
            act["jump"] = obs["tick"] % 18 == 0

    def _exec_fight(self, obs, act):
        me = obs["self"]
        tgt = self._best_fight_target(obs)
        if not tgt:
            self._exec_zone(obs, act)
            return
        d = self._dist2(me["pos"], tgt["pos"])
        pred = self._lead(me, tgt)
        act["aim"] = self._aim_at(me, pred)
        ideal = 28.0 if self._secondary_kind(me) == "lightning" else 45.0
        if d > ideal + 8:
            act["move"] = self._strafe(self._toward(me["pos"], pred))
            act["boost"] = d > 55
        elif d < ideal - 10:
            away = self._norm2([me["pos"][0] - tgt["pos"][0], me["pos"][1] - tgt["pos"][1]])
            act["move"] = self._strafe(away)
        else:
            act["move"] = self._strafe(self._toward(me["pos"], pred))
        act["fire"] = self._fire_slots(me, d)
        if any(act["fire"]):
            mf.draw.line(me["pos"], tgt["pos"], "#ff5040")

    def _exec_flee(self, obs, act):
        enemies = self._enemies(obs)
        if enemies:
            act["move"] = self._strafe(self._flee_dir(obs, enemies))
            act["aim"] = self._aim_at(obs["self"], enemies[0]["pos"])
        else:
            self._exec_zone(obs, act)
        act["boost"] = True
        if obs["tick"] % 14 == 0:
            act["jump"] = True

    def _exec_loot_weapon(self, obs, act):
        me = obs["self"]
        picks = [p for p in obs["pickups"] if p["kind"] != "gun"]
        if not picks:
            self._exec_zone(obs, act)
            return
        dest = min(picks, key=lambda p: self._dist2(me["pos"], p["pos"]))
        act["move"] = self._strafe(self._toward(me["pos"], dest["pos"]))
        act["boost"] = self._dist2(me["pos"], dest["pos"]) > 35
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
        act["move"] = self._strafe(self._toward(me["pos"], dest["pos"]))
        act["boost"] = self._dist2(me["pos"], dest["pos"]) > 40
        if "hp" in dest:
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
        act["move"] = self._strafe(self._toward(me["pos"], pred) if d > 25 else self._norm2(
            [me["pos"][0] - tgt["pos"][0], me["pos"][1] - tgt["pos"][1]]))
        act["aim"] = self._aim_at(me, pred)
        act["fire"] = self._fire_slots(me, d)

    def _exec_highground(self, obs, act):
        me = obs["self"]
        enemies = self._enemies(obs)
        if enemies:
            act["aim"] = self._aim_at(me, enemies[0]["pos"])
        base = self._toward(me["pos"], self._zone_goal(obs))
        act["move"] = base
        act["boost"] = True
        if me.get("grounded") and obs["tick"] % 25 == 0:
            act["jump"] = True

    def _update_state(self, obs):
        me = obs["self"]
        for e in obs.get("events", []):
            if e.get("type") == "kill":
                self.kill_count += 1
        if self.last_pos and self._dist2(me["pos"], self.last_pos) < 0.1:
            self.stuck_ticks += 1
        else:
            self.stuck_ticks = 0
        self.last_pos = list(me["pos"])

    def act(self, obs: mf.Observation) -> mf.Action:
        act: mf.Action = {"tick": obs["tick"]}
        me = obs["self"]
        self._update_state(obs)

        if obs["offer"]:
            act["pick"] = self._choose_upgrade(obs["offer"], me)

        pickup, drop_slot = self._try_pickup(me, obs["pickups"])
        if pickup:
            act["take"] = pickup["id"]
            if drop_slot is not None:
                act["drop"] = drop_slot

        if self.stuck_ticks > 20:
            a = self.rng.uniform(0, 2 * math.pi)
            act.update(move=[math.cos(a), math.sin(a)], boost=True, jump=True)
            self.stuck_ticks = 0
            mf.draw.label("UNSTICK")
            return act

        scores = {
            "zone": self._score_zone(obs),
            "fight": self._score_fight(obs),
            "flee": self._score_flee(obs),
            "loot_weapon": self._score_loot_weapon(obs),
            "loot_cubes": self._score_loot_cubes(obs),
            "hunt_npc": self._score_hunt_npc(obs),
            "highground": self._score_highground(obs),
        }
        behavior = max(scores, key=scores.get)
        self.current_behavior = behavior

        executors = {
            "zone": self._exec_zone,
            "fight": self._exec_fight,
            "flee": self._exec_flee,
            "loot_weapon": self._exec_loot_weapon,
            "loot_cubes": self._exec_loot_cubes,
            "hunt_npc": self._exec_hunt_npc,
            "highground": self._exec_highground,
        }
        executors[behavior](obs, act)

        if "move" not in act:
            act["move"] = [0.0, 0.0]
        move, boost = self._apply_dodge(obs, act.get("move", [0.0, 0.0]), act.get("boost", False))
        act["move"] = move
        if boost:
            act["boost"] = True

        if "aim" not in act:
            enemies = self._enemies(obs)
            if enemies:
                act["aim"] = self._aim_at(me, enemies[0]["pos"])
            else:
                act["aim"] = act.get("aim", me.get("aim", [0.0, 0.0]))

        w0 = me["weapons"][0]
        if w0 and w0["kind"] == "gun" and w0.get("ammo", 1) == 0 and not act.get("fire", [False])[0]:
            act["reload"] = True

        margin = self._zone_margin(obs)
        top = sorted(scores.items(), key=lambda x: -x[1])[:3]
        mf.draw.label(
            f"{behavior} | hp {self._hp_ratio(me):.0%} | "
            f"scores {top[0][1]:.0f}/{top[1][1]:.0f} | zone {margin:+.0f}m | "
            f"k{self.kill_count} c{me['cubes']}"
        )
        return act


mf.run(Bot)