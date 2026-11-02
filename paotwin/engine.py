"""The state engine: messages in, a state per signal and per zone out.

Pure logic, no sockets and no clock of its own (``now`` is passed in), so
every rule can be tested exactly.

States, worst first. A zone takes the worst state among its signals:

    loi            Lỗi             a value past its fault threshold
    mat_tin_hieu   Mất tín hiệu    no fresh, trustworthy value (never "normal")
    canh_bao       Cảnh báo        a value past its warning threshold
    binh_thuong    Bình thường     a fresh value inside its thresholds
    chua_co        Chưa có cảm biến nothing measures this part yet

"Mất tín hiệu" ranks above "Cảnh báo" on purpose: a part we cannot see may be
in any state, and a maintenance person should look at it before a part that
is only warm.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass, field

PREFIX = "paotwin/"
GATEWAY_STATUS = PREFIX + "gateway/trang_thai"
GPU_LIMITS = PREFIX + "laptop/gpu_gioi_han"

LOI, MAT, CANH_BAO, BINH_THUONG, CHUA_CO = (
    "loi", "mat_tin_hieu", "canh_bao", "binh_thuong", "chua_co")
SEVERITY = {LOI: 4, MAT: 3, CANH_BAO: 2, BINH_THUONG: 1, CHUA_CO: 0}
LABELS = {LOI: "Lỗi", MAT: "Mất tín hiệu", CANH_BAO: "Cảnh báo",
          BINH_THUONG: "Bình thường", CHUA_CO: "Chưa có cảm biến"}

LIMIT_NAMES = {"max_operating": "Max Operating", "slowdown": "Slowdown",
               "shutdown": "Shutdown", "target": "Target"}

LATENCY_SAMPLES = 300
HISTORY_POINTS = 1000          # about 33 minutes at one reading every 2 s
CLOCK_SKEW_ALLOWANCE_S = 2.0   # a reading stamped this far in the future is still taken

# Machine-level states, in the words the M64 PoC used.
MAY_GIA_CONG, MAY_CHO, MAY_TAT, MAY_LOI, MAY_KHONG_RO = (
    "gia_cong", "cho", "tat", "loi", "khong_ro")
MACHINE_LABELS = {MAY_GIA_CONG: "Gia công", MAY_CHO: "Chờ", MAY_TAT: "Tắt", MAY_LOI: "Lỗi",
                  MAY_KHONG_RO: "Không rõ: tắt máy hoặc mất tín hiệu"}
EVENT_LIMIT = 200


@dataclass
class _Latest:
    value: object = None
    quality_ok: bool = False
    reason: str | None = None
    read_ms: float | None = None      # when the gateway read it (gateway clock)
    received_s: float | None = None   # when this server received it (server clock)


@dataclass
class SignalState:
    id: str
    state: str
    value: object = None
    text: str = ""
    reason: str = ""
    threshold: dict | None = None
    age_s: float | None = None


@dataclass
class ZoneState:
    id: str
    state: str
    reason: str = ""
    signal_ids: list[str] = field(default_factory=list)


def _fmt(value: float, digits: int) -> str:
    text = f"{value:.{digits}f}"
    return text.replace(".", ",")


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


class Engine:
    def __init__(self, config: dict, model: dict) -> None:
        self.config, self.model = config, model
        self.stale_after_s = float(config["mat_tin_hieu_sau_s"])
        self.signals = {s["id"]: s for s in config["tin_hieu"]}
        self.zones = {z["id"]: z for z in model["vung"]}
        self.zone_signals: dict[str, list[str]] = {z: [] for z in self.zones}
        for sid, spec in self.signals.items():
            zone = spec["vung"]
            if zone not in self.zones:
                raise ValueError(f"tín hiệu {sid} trỏ tới vùng không có: {zone}")
            self.zone_signals[zone].append(sid)
        self.latest: dict[str, _Latest] = {}
        self.gateway_online: bool | None = None  # None: never heard of it
        self.gateway_since_s: float | None = None
        self.broker_connected = False
        self.gpu_limits: dict[str, float] = {}
        self.latency_ms: deque[float] = deque(maxlen=LATENCY_SAMPLES)
        self.events: deque[dict] = deque(maxlen=EVENT_LIMIT)
        self.messages = 0
        self.version = 0
        self._zone_states: dict[str, str] = {}
        self._lock = threading.Lock()
        self._evaluated: tuple[dict[str, SignalState], dict[str, ZoneState]] | None = None
        self.history: dict[str, deque[tuple[float, float]]] = {}
        self._derived_seen: dict[str, float] = {}
        self.machines = config.get("may", [])
        self._machine_state: dict[str, tuple[str, float]] = {}   # id -> (state, since)
        self._machine_time: dict[str, dict[str, float]] = {}     # id -> state -> seconds
        self._machine_tick: dict[str, float] = {}
        self.started_s: float | None = None

    # -- input ----------------------------------------------------------------

    def set_broker_connected(self, connected: bool, now: float) -> None:
        with self._lock:
            self.broker_connected = connected
            self.version += 1

    def on_message(self, topic: str, payload: bytes, now: float) -> None:
        with self._lock:
            self.messages += 1
            self.version += 1
            if topic == GATEWAY_STATUS:
                online = payload.strip() == b"online"
                if online != self.gateway_online:
                    self.gateway_since_s = now
                self.gateway_online = online
                return
            if topic == GPU_LIMITS:
                try:
                    data = json.loads(payload)
                    self.gpu_limits = {k: float(v) for k, v in data.items()
                                       if isinstance(v, (int, float)) and k != "t"}
                except (ValueError, TypeError, AttributeError):
                    self.gpu_limits = {}
                return
            if not topic.startswith(PREFIX):
                return
            sid = topic[len(PREFIX):]
            if sid not in self.signals:
                return
            latest = self.latest.setdefault(sid, _Latest())
            latest.received_s = now
            try:
                data = json.loads(payload)
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except ValueError:
                latest.value, latest.quality_ok = None, False
                latest.reason, latest.read_ms = "dữ liệu nhận được bị hỏng", None
                return
            latest.read_ms = data.get("t") if isinstance(data.get("t"), (int, float)) else None
            latest.quality_ok = data.get("q") == "tot"
            latest.value = data.get("v") if latest.quality_ok else None
            latest.reason = data.get("ly_do") if not latest.quality_ok else None
            if latest.read_ms is not None:
                self.latency_ms.append(now * 1000.0 - latest.read_ms)
            value = latest.value
            if latest.quality_ok and isinstance(value, (int, float)) and not isinstance(value, bool):
                stamp = latest.read_ms / 1000.0 if latest.read_ms is not None else now
                self._remember(sid, stamp, float(value))

    def _remember(self, sid: str, t: float, value: float) -> None:
        points = self.history.setdefault(sid, deque(maxlen=HISTORY_POINTS))
        points.append((t, value))

    def history_of(self, sid: str, since: float | None = None) -> list[tuple[float, float]]:
        with self._lock:
            points = list(self.history.get(sid, ()))
        if since is not None:
            points = [p for p in points if p[0] >= since]
        return points

    # -- rules ----------------------------------------------------------------

    def _threshold(self, spec: dict) -> dict | None:
        rule = spec.get("nguong")
        if not rule:
            return None
        if rule["nguon"] == "thiet_bi":
            warn = self.gpu_limits.get(rule["canh_bao_tu"])
            fault = self.gpu_limits.get(rule["loi_tu"])
            if warn is None and fault is None:
                return None
            note = rule.get("giai_thich", "")
            reported = ", ".join(f"{LIMIT_NAMES.get(k, k)} {v:g}"
                                 for k, v in sorted(self.gpu_limits.items(), key=lambda kv: kv[1]))
            if reported:
                note += f" Card báo: {reported}."
            above = rule["huong"] == "tren"
            if warn is not None and fault is not None and (warn >= fault if above else warn <= fault):
                # The card's own numbers are not in the order the rule expects
                # (seen on the RTX 3050 Laptop: Max Operating 105, Slowdown 97).
                # No warning level is made up to replace it.
                note += (f" Mức cảnh báo ({LIMIT_NAMES.get(rule['canh_bao_tu'])} {warn:g}) không thấp hơn"
                         f" mức lỗi ({LIMIT_NAMES.get(rule['loi_tu'])} {fault:g}) nên không dùng;"
                         " vùng này chỉ có mức lỗi.")
                warn = None
            return {"nguon": "thiet_bi", "huong": rule["huong"], "canh_bao": warn,
                    "loi": fault, "giai_thich": note.strip()}
        return {"nguon": rule["nguon"], "huong": rule["huong"],
                "canh_bao": rule.get("canh_bao"), "loi": rule.get("loi"),
                "giai_thich": rule.get("giai_thich", "")}

    @staticmethod
    def _against(value: float, threshold: dict | None) -> str:
        if threshold is None:
            return BINH_THUONG
        above = threshold["huong"] == "tren"

        def past(limit):
            if limit is None:
                return False
            return value >= limit if above else value <= limit

        if past(threshold.get("loi")):
            return LOI
        if past(threshold.get("canh_bao")):
            return CANH_BAO
        return BINH_THUONG

    def _link_problem(self) -> str | None:
        if not self.broker_connected:
            return "Máy chủ bản sao mất kết nối broker MQTT"
        if self.gateway_online is None:
            return "Chưa nghe thấy gateway"
        if not self.gateway_online:
            return "Gateway mất kết nối (broker đã phát Last Will)"
        return None

    def _fresh(self, sid: str, now: float) -> tuple[_Latest | None, str | None, float | None]:
        latest = self.latest.get(sid)
        if latest is None or latest.received_s is None:
            return None, "Chưa nhận được dữ liệu", None
        age = now - latest.received_s
        if age > self.stale_after_s:
            return latest, f"Quá {_fmt(self.stale_after_s, 0)} s không có dữ liệu mới", age
        if latest.read_ms is not None:
            # A reading that arrives late is as old as when it was taken, not
            # as when it arrived. Assumes the gateway's clock is synchronised
            # (same machine here; NTP on a real site).
            measured_age = now - latest.read_ms / 1000.0
            if measured_age > self.stale_after_s:
                return latest, (f"Số đo đến trễ: đã cũ {_fmt(measured_age, 0)} s"
                                f" (quá {_fmt(self.stale_after_s, 0)} s)"), measured_age
            if measured_age >= -CLOCK_SKEW_ALLOWANCE_S:
                age = max(age, measured_age)
        if not latest.quality_ok:
            return latest, latest.reason or "Gateway báo không đọc được", age
        return latest, None, age

    def _signal(self, sid: str, now: float, link: str | None) -> SignalState:
        spec = self.signals[sid]
        kind = spec["kieu"]
        digits = int(spec.get("so_le", 1))
        unit = spec.get("don_vi", "")
        if kind == "chua_co":
            return SignalState(sid, CHUA_CO, reason=spec.get("ly_do", ""))
        if kind == "ket_noi":
            if not self.broker_connected:
                return SignalState(sid, MAT, text="Không rõ",
                                   reason="Máy chủ bản sao mất kết nối broker MQTT")
            if self.gateway_online:
                return SignalState(sid, BINH_THUONG, value="online", text="Đang kết nối")
            if self.gateway_online is None:
                return SignalState(sid, MAT, text="Chưa thấy", reason="Chưa nghe thấy gateway")
            return SignalState(sid, MAT, value="offline", text="Mất kết nối",
                               reason="Broker đã phát Last Will của gateway")
        threshold = self._threshold(spec)
        if link is not None:
            return SignalState(sid, MAT, reason=link, threshold=threshold)
        if kind == "lech_pha":
            return self._imbalance(sid, spec, now, threshold, digits)
        latest, problem, age = self._fresh(sid, now)
        if problem is not None:
            return SignalState(sid, MAT, reason=problem, threshold=threshold, age_s=age)
        value = latest.value
        if kind == "liet_ke":
            option = spec["liet_ke"].get(value)
            if option is None:
                return SignalState(sid, MAT, value=value, reason=f"Giá trị lạ: {value!r}",
                                   age_s=age)
            return SignalState(sid, option["trang_thai"], value=value, text=option["nhan"],
                               age_s=age)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return SignalState(sid, MAT, reason="Giá trị không phải số", age_s=age)
        state = self._against(float(value), threshold)
        text = f"{_fmt(float(value), digits)} {unit}".strip()
        reason = ""
        if threshold is None and spec.get("nguong"):
            reason = "Thiết bị không báo ngưỡng, chỉ hiện số"
        return SignalState(sid, state, value=value, text=text, threshold=threshold,
                           age_s=age, reason=reason)

    def _imbalance(self, sid: str, spec: dict, now: float, threshold: dict | None,
                   digits: int) -> SignalState:
        values, oldest = [], 0.0
        for source in spec["tu"]:
            latest, problem, age = self._fresh(source, now)
            if problem is not None:
                name = self.signals[source]["ten"]
                return SignalState(sid, MAT, reason=f"Thiếu {name}: {problem}",
                                   threshold=threshold, age_s=age)
            if isinstance(latest.value, bool) or not isinstance(latest.value, (int, float)):
                return SignalState(sid, MAT, reason="Giá trị pha không phải số", age_s=age)
            values.append(float(latest.value))
            oldest = max(oldest, age or 0.0)
        mean = sum(values) / len(values)
        if mean < float(spec.get("dang_dung_duoi", 0.0)):
            return SignalState(sid, BINH_THUONG, text="Không tính (động cơ đang dừng)",
                               threshold=threshold, age_s=oldest)
        value = max(abs(v - mean) for v in values) / mean * 100.0
        newest = max(self.latest[source].received_s for source in spec["tu"])
        if self._derived_seen.get(sid) != newest:
            self._derived_seen[sid] = newest
            self._remember(sid, now, round(value, 3))
        state = self._against(value, threshold)
        return SignalState(sid, state, value=round(value, 3),
                           text=f"{_fmt(value, digits)} %", threshold=threshold, age_s=oldest)

    # -- output ---------------------------------------------------------------

    def evaluate(self, now: float) -> tuple[dict[str, SignalState], dict[str, ZoneState]]:
        """Recomputes every state; records an event for each zone that changed."""
        with self._lock:
            link = self._link_problem()
            signals = {sid: self._signal(sid, now, link) for sid in self.signals}
            zones = {}
            for zid, sids in self.zone_signals.items():
                measured = [signals[s] for s in sids if signals[s].state != CHUA_CO]
                if not measured:
                    reason = "; ".join(signals[s].reason for s in sids if signals[s].reason)
                    zones[zid] = ZoneState(zid, CHUA_CO, reason or "Chưa gắn cảm biến nào", sids)
                    continue
                worst = max(measured, key=lambda s: SEVERITY[s.state])
                name = self.signals[worst.id]["ten"]
                detail = worst.reason or worst.text
                reason = f"{name}: {detail}" if worst.state != BINH_THUONG else ""
                same = sum(1 for s in measured if s.state == worst.state) - 1
                if reason and same:
                    reason += f" (và {same} tín hiệu khác cùng mức)"
                zones[zid] = ZoneState(zid, worst.state, reason, sids)
            for zid, zone in zones.items():
                before = self._zone_states.get(zid)
                if before is not None and before != zone.state:
                    self.events.appendleft({
                        "t": now, "vung": zid, "ten_vung": self.zones[zid]["ten"],
                        "tu": before, "den": zone.state, "ly_do": zone.reason})
                    self.version += 1
                self._zone_states[zid] = zone.state
            self._evaluated = (signals, zones)
            if self.started_s is None:
                self.started_s = now
            self._machines(signals, now)
            return signals, zones

    def _machine_state_now(self, spec: dict, signals: dict[str, SignalState]) -> tuple[str, str]:
        lamp = signals.get(spec["den"])
        if lamp is None or lamp.state == MAT:
            reason = lamp.reason if lamp is not None else "Không có tín hiệu đèn"
            return MAY_KHONG_RO, reason
        state = {"loi": MAY_LOI, "chay": MAY_GIA_CONG, "cho": MAY_CHO, "tat": MAY_TAT}.get(
            lamp.value, MAY_KHONG_RO)
        note = ""
        currents = [signals[s] for s in spec.get("dong", []) if s in signals]
        known = [float(c.value) for c in currents
                 if c.state != MAT and isinstance(c.value, (int, float))]
        limit = float(spec.get("dong_chay_tu", 0.5))
        if state == MAY_GIA_CONG and known and max(known) < limit:
            note = "Đèn báo chạy nhưng động cơ không có dòng"
        if state in (MAY_CHO, MAY_TAT) and known and max(known) >= limit:
            note = "Đèn báo không chạy nhưng động cơ đang có dòng"
        return state, note

    def _machines(self, signals: dict[str, SignalState], now: float) -> None:
        for spec in self.machines:
            mid = spec["id"]
            state, _note = self._machine_state_now(spec, signals)
            times = self._machine_time.setdefault(mid, {k: 0.0 for k in MACHINE_LABELS})
            before = self._machine_state.get(mid)
            last = self._machine_tick.get(mid)
            if before is not None and last is not None and now > last:
                times[before[0]] += now - last
            self._machine_tick[mid] = now
            if before is None or before[0] != state:
                if before is not None:
                    self.events.appendleft({
                        "t": now, "may": mid, "ten_vung": spec["ten"],
                        "tu_may": before[0], "den_may": state,
                        "ly_do": MACHINE_LABELS[before[0]] + " thành " + MACHINE_LABELS[state]})
                    self.version += 1
                self._machine_state[mid] = (state, now)

    def machines_snapshot(self, signals: dict[str, SignalState]) -> list[dict]:
        out = []
        for spec in self.machines:
            mid = spec["id"]
            state, note = self._machine_state_now(spec, signals)
            since = self._machine_state.get(mid, (state, None))[1]
            out.append({"id": mid, "ten": spec["ten"], "vung": spec.get("vung", []),
                        "trang_thai": state, "nhan": MACHINE_LABELS[state], "ghi_chu": note,
                        "tu_luc": since,
                        "thoi_gian_s": {k: round(v, 1) for k, v in
                                        self._machine_time.get(mid, {}).items()},
                        "tinh_tu": self.started_s})
        return out

    def latency(self) -> dict:
        with self._lock:
            samples = sorted(self.latency_ms)
        if not samples:
            return {"so_mau": 0}
        return {"so_mau": len(samples), "p50_ms": round(_percentile(samples, 0.5), 1),
                "p95_ms": round(_percentile(samples, 0.95), 1),
                "max_ms": round(samples[-1], 1)}

    def snapshot(self, now: float) -> dict:
        signals, zones = self.evaluate(now)
        counts = {state: 0 for state in SEVERITY}
        for zone in zones.values():
            counts[zone.state] += 1
        with self._lock:
            events = list(self.events)[:50]
            gateway = {"online": self.gateway_online,
                       "doi_trang_thai_luc": self.gateway_since_s}
            broker = self.broker_connected
            messages = self.messages
            limits = dict(self.gpu_limits)
            machines = self.machines_snapshot(signals)
        return {
            "t": now,
            "broker": broker,
            "gateway": gateway,
            "so_tin_nhan": messages,
            "gioi_han_gpu": limits,
            "may": machines,
            "dem": counts,
            "vung": {zid: {"trang_thai": z.state, "nhan": LABELS[z.state],
                           "ly_do": z.reason, "tin_hieu": z.signal_ids}
                     for zid, z in zones.items()},
            "tin_hieu": {sid: {"trang_thai": s.state, "nhan": LABELS[s.state],
                               "gia_tri": s.value, "chu": s.text, "ly_do": s.reason,
                               "nguong": s.threshold, "tuoi_s": s.age_s}
                         for sid, s in signals.items()},
            "do_tre": self.latency(),
            "su_kien": events,
        }
