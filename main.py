import gc
import math
import os
import select
import socket
import time
import ujson
from machine import ADC, I2C, Pin, RTC, WDT, reset
import network
import rp2
import ssd1306
import micropython

print("=" * 50)
print("Khởi động Pico W SmartPSU...")
print("=" * 50)

gc.collect()

rtc = RTC()

def url_unquote(s):
    if not s or "%" not in s:
        return s
    res = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "%" and i + 2 < n:
            try:
                res.append(chr(int(s[i+1:i+3], 16)))
                i += 3
                continue
            except Exception:
                pass
        res.append(s[i])
        i += 1
    return "".join(res)

def get_real_time_str():
    try:
        dt = rtc.datetime()
        return f"{dt[0]:04d}-{dt[1]:02d}-{dt[2]:02d} {dt[4]:02d}:{dt[5]:02d}:{dt[6]:02d}"
    except Exception:
        tm = time.localtime()
        return f"{tm[0]:04d}-{tm[1]:02d}-{tm[2]:02d} {tm[3]:02d}:{tm[4]:02d}:{tm[5]:02d}"

def set_pico_time(time_data):
    global measurement_records
    try:
        s = url_unquote(str(time_data)).replace("%2C", ",").replace("%2c", ",").strip()
        s = s.replace("-", ",").replace(":", ",").replace(" ", ",").replace("_", ",")
        if "," in s:
            parts = [int(p) for p in s.split(",") if p.isdigit()]
            if len(parts) >= 6:
                y, m, d = parts[0], parts[1], parts[2]
                wd = parts[3] if len(parts) >= 7 else 0
                h = parts[4] if len(parts) >= 7 else parts[3]
                mn = parts[5] if len(parts) >= 7 else parts[4]
                sec = parts[6] if len(parts) >= 7 else parts[5]
                rtc.datetime((y, m, d, wd, h, mn, sec, 0))
                now_str = get_real_time_str()
                print(f"[RTC] Synced successfully: {now_str}")
                if y >= 2024:
                    for rec in measurement_records:
                        if rec.get("datetime", "").startswith("2021"):
                            rec["datetime"] = now_str
                return
        epoch_sec = float(s)
        tm = time.localtime(int(epoch_sec))
        rtc.datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))
        now_str = get_real_time_str()
        print(f"[RTC] Synced epoch: {now_str}")
        if tm[0] >= 2024:
            for rec in measurement_records:
                if rec.get("datetime", "").startswith("2021"):
                    rec["datetime"] = now_str
    except Exception as e:
        print("[RTC] Err:", e)

ACCOUNTS = {
    "admin1": ["1", "admin1"],
    "admin2": ["2", "admin2"],
    "admin3": ["3", "admin3"],
    "admin4": ["4", "admin4"]
}

active_tokens = {}
MIN_DUTY = 0.0
MAX_DUTY = 40.0
MAX_TEMP_LIMIT = 75.0

MAX_LOGS = 20
LOG_FILE = "system_logs.json"
LOG_SAVE_INTERVAL_MS = 5000
system_logs = []
log_seq = 0
logs_dirty = False
last_log_save = 0

measurement_records = []
MAX_MEASURE_RECORDS = 20
last_record_time = 0

latest_rec_push = None
latest_log_push = None

def load_logs():
    global system_logs, log_seq
    # Khởi tạo danh sách nhật ký trong RAM (không đọc/ghi Flash định kỳ)
    system_logs = []
    log_seq = 1

def save_logs():
    global logs_dirty
    logs_dirty = False
    # BẢO VỆ TUỔI THỌ FLASH PICO W TUYỆT ĐỐI:
    # Dữ liệu đo đạc và nhật ký được lưu trên RAM Pico W và truyền thời gian thực
    # xuống lưu trữ an toàn trên PC/Điện thoại. Pico W hoàn toàn không phải ghi Flash.
    pass

def add_log(log_type, user_str, msg_str):
    global log_seq, logs_dirty, latest_log_push
    t_str = get_real_time_str()
    log_entry = {"t": t_str, "type": log_type, "user": user_str, "msg": msg_str}
    system_logs.insert(0, log_entry)
    if len(system_logs) > MAX_LOGS:
        system_logs.pop()
    log_seq += 1
    logs_dirty = True
    latest_log_push = log_entry

load_logs()

def add_measurement_record():
    global latest_rec_push
    time_str = get_real_time_str()
    is_overtemp = telemetry_cache["temp"] > MAX_TEMP_LIMIT
    status_str = "QUÁ NHIỆT" if is_overtemp else "OK"

    rec = {
        "datetime": time_str,
        "v_out": telemetry_cache["v_out"],
        "i_out": telemetry_cache["i_out"],
        "p_out": telemetry_cache["p_out"],
        "temp": telemetry_cache["temp"],
        "duty": telemetry_cache["d"],
        "status": status_str
    }
    measurement_records.insert(0, rec)
    if len(measurement_records) > MAX_MEASURE_RECORDS:
        measurement_records.pop()
    latest_rec_push = rec

    if is_overtemp:
        add_log("alarm", "Hệ thống", f"Cảnh báo nhiệt độ vi điều khiển vượt ngưỡng: {telemetry_cache['temp']:.1f}°C")

def parse_query_params(req_line):
    params = {}
    if "?" in req_line:
        try:
            query_str = req_line.split("?")[1].split(" ")[0]
            pairs = query_str.split("&")
            for pair in pairs:
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = v
        except Exception:
            pass
    return params

AP_SSID = "PicoW_PushPull"
AP_PASS = "12345678"

ap = network.WLAN(network.AP_IF)
ap.active(False)
time.sleep(0.1)
ap.active(True)

ap.ifconfig(('192.168.4.1', '255.255.255.0', '192.168.4.1', '8.8.8.8'))

try:
    ap.config(essid=AP_SSID, password=AP_PASS, channel=6, authmode=3)
except Exception:
    ap.config(essid=AP_SSID, password=AP_PASS)

try:
    # 0xa11140: Tắt hoàn toàn tiết kiệm năng lượng CYW43 để Access Point đạt hiệu năng cao nhất, không rớt gói
    ap.config(pm=0xa11140)
except Exception:
    pass

while not ap.active():
    time.sleep(0.1)

ip_address = ap.ifconfig()[0]
print("=" * 50)
print(f" FAST WORKSPACE SSE READY: http://{ip_address}")
print("=" * 50)

class INA226:
    def __init__(self, i2c, addr=0x40, r_shunt=0.1):
        self.i2c = i2c
        self.addr = addr
        self.r_shunt = r_shunt
        self.is_alive = False
        self.init_sensor()

    def init_sensor(self):
        try:
            m_id = self._read_reg(0xFE)
            d_id = self._read_reg(0xFF)
            if m_id == 0x5449 and (d_id & 0xFFF0) == 0x2260:
                self.is_alive = True
                self._write_reg(0x00, 0x4127)
            else:
                self.is_alive = False
        except Exception:
            self.is_alive = False
        
    def _write_reg(self, reg, val):
        buf = bytes([reg, (val >> 8) & 0xFF, val & 0xFF])
        self.i2c.writeto(self.addr, buf)
        
    def _read_reg(self, reg):
        self.i2c.writeto(self.addr, bytes([reg]))
        data = self.i2c.readfrom(self.addr, 2)
        val = (data[0] << 8) | data[1]
        if val & 0x8000: val -= 65536
        return val
        
    def read_bus_voltage(self):
        v_reg = self._read_reg(0x02)
        if v_reg < 0: v_reg = 0
        return v_reg * 0.00125
        
    def read_current(self):
        v_shunt = self._read_reg(0x01)
        v_shunt_volts = v_shunt * 0.0000025
        curr = abs(v_shunt_volts) / self.r_shunt
        return curr if curr > 0.005 else 0.0

i2c_bus = I2C(0, sda=Pin(16), scl=Pin(17), freq=400000)

oled_available = False
oled = None
try:
    oled = ssd1306.SSD1306_I2C(128, 64, i2c_bus, addr=0x3C)
    oled_available = True
except Exception: pass

ina_available = False
ina = None
try:
    ina = INA226(i2c_bus, addr=0x40, r_shunt=0.1)
    if ina.is_alive:
        ina_available = True
        print("[INA226] Chip OK (TI INA226 Detected)")
    else:
        print("[INA226] Sensor Check Failed or Not Responding!")
except Exception as e:
    print("[INA226] Init Err:", e)

MIN_DUTY = 0
MAX_DUTY = 40.0
def is_data_file(name):
    # Loại bỏ file log hệ thống, manifest, và file lut_12 cũ theo yêu cầu bài thi
    if not name.endswith(".json"): return False
    if name == LOG_FILE or name == "manifest.json": return False
    if name.startswith("lut_12") or name == "lut_12.json": return False
    return True

def get_lut_files_list():
    try:
        files = sorted([f for f in os.listdir() if is_data_file(f)])
        return files
    except Exception:
        return []

_init_files = get_lut_files_list()
LUT_FILE = _init_files[0] if _init_files else "tai_5V.json"
pio_is_off = False
v_grid = []
d_grid = []
tuning_raw_data = []
pending_sweep_results = []

DEFAULT_V_GRID = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
DEFAULT_D_GRID = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0, 26.0, 28.0, 30.0]

def load_20pts_lut(data=None):
    global v_grid, d_grid, tuning_raw_data, LUT_FILE
    if LUT_FILE.startswith("lut_12") or LUT_FILE == "manifest.json":
        _avail = get_lut_files_list()
        LUT_FILE = _avail[0] if _avail else "tai_5V.json"
    if data is None:
        try:
            with open(LUT_FILE, "r") as f:
                data = ujson.load(f)
        except Exception: pass
    
    if data and "raw" in data:
        tuning_raw_data = data["raw"]
    elif isinstance(data, list):
        tuning_raw_data = data
    else:
        tuning_raw_data = []
    
    if len(tuning_raw_data) > 0:
        v_tmp, d_tmp = [], []
        for item in tuning_raw_data:
            if "volt" in item and "duty" in item:
                v_tmp.append(round(float(item["volt"]), 3))
                d_tmp.append(round(float(item["duty"]), 2))
        v_grid = v_tmp
        d_grid = d_tmp
    else:
        v_grid = list(DEFAULT_V_GRID)
        d_grid = list(DEFAULT_D_GRID)
    try:
        telemetry_cache["lut_file"] = LUT_FILE
    except Exception: pass

def save_20pts_lut(results):
    payload = {"raw": results}
    try:
        with open(LUT_FILE, "w") as f:
            ujson.dump(payload, f)
    except Exception as e:
        print("[LUT Save Error]", e)
    load_20pts_lut(payload)

load_20pts_lut()

def estimate_duty_adaptive(v_tgt):
    if v_tgt <= 0.01: return 0.0
    if not v_grid or not d_grid: return MIN_DUTY
    if v_tgt <= v_grid[0]: return d_grid[0]
    if v_tgt >= v_grid[-1]: return d_grid[-1]

    for i in range(len(v_grid) - 1):
        v0, v1 = v_grid[i], v_grid[i + 1]
        if v0 <= v_tgt <= v1:
            if abs(v1 - v0) < 1e-4: return d_grid[i]
            d0, d1 = d_grid[i], d_grid[i + 1]
            ratio = (v_tgt - v0) / (v1 - v0)
            return round(d0 + ratio * (d1 - d0), 2)
    return d_grid[0]

@rp2.asm_pio(
    set_init=(rp2.PIO.OUT_LOW, rp2.PIO.OUT_LOW),
    out_shiftdir=rp2.PIO.SHIFT_LEFT,
)
def dual_phase_pwm_variable():
    wrap_target()
    pull(noblock)
    mov(x, osr)
    out(y, 16)
    mov(isr, y)
    set(pins, 0b01)
    mov(y, isr)
    label("on1")
    jmp(y_dec, "on1")
    set(pins, 0b00)
    mov(osr, x)
    out(null, 16)
    out(y, 16)
    label("off1")
    jmp(y_dec, "off1")
    set(pins, 0b10)
    mov(y, isr)
    label("on2")
    jmp(y_dec, "on2")
    set(pins, 0b00)
    mov(osr, x)
    out(null, 16)
    out(y, 16)
    label("off2")
    jmp(y_dec, "off2")
    wrap()

PIO_FREQ = 100_000_000
sm = rp2.StateMachine(0, dual_phase_pwm_variable, freq=PIO_FREQ, set_base=Pin(14))

def update_duty(duty_percent):
    global pio_is_off
    if duty_percent > MAX_DUTY: duty_percent = MAX_DUTY
    elif duty_percent < MIN_DUTY: duty_percent = MIN_DUTY

    if duty_percent <= 0.0:
        if not pio_is_off:
            sm.active(0)
            sm.exec(0xE000)
            pio_is_off = True
        return

    n_on = int(round(50 * duty_percent - 3))
    if n_on < 1: n_on = 1
    n_off = 2492 - n_on
    if n_off < 1: n_off = 1
    word = ((n_on & 0xFFFF) << 16) | (n_off & 0xFFFF)

    if pio_is_off:
        sm.init(dual_phase_pwm_variable, freq=PIO_FREQ, set_base=Pin(14))
        pio_is_off = False

    sm.put(word)
    sm.active(1)

@micropython.viper
def capture_highspeed(buf1, buf2, num_samples: int):
    reg = ptr32(0xD0000004)
    p1 = ptr8(buf1)
    p2 = ptr8(buf2)
    t = 50000
    while (reg[0] & (1 << 14)) and t > 0: t -= 1
    t = 50000
    while not (reg[0] & (1 << 14)) and t > 0: t -= 1
    for i in range(num_samples):
        v = reg[0]
        p1[i] = (v >> 14) & 1
        p2[i] = (v >> 15) & 1
        k = 10
        while k > 0: k -= 1

adc_voltage_pin = ADC(26)
adc_temp_pin = ADC(27)

R_REF_NTC = 6800.0
R0_NTC = 6500
BETA_NTC = 3950.0
T0_KELVIN = 298.15

v_adc_filtered = 0.0
v_out_filtered = 0.0
temp_filtered = 0.0
i_out_filtered = 0.0
p_out_filtered = 0.0
ALPHA_V    = 0.20
ALPHA_TEMP = 0.05
ALPHA_I    = 0.35

MAX_CURRENT_LIMIT = 5.0
MAX_TEMP_LIMIT = 75.0
is_overcurrent_tripped = False
is_overtemp_tripped = False

def read_telemetry():
    global v_adc_filtered, v_out_filtered, temp_filtered, i_out_filtered, p_out_filtered
    global is_overcurrent_tripped, is_overtemp_tripped, current_duty, control_mode

    acc_v = 0
    for _ in range(4): acc_v += adc_voltage_pin.read_u16()
    v_adc_inst = (acc_v / 262140.0) * 3.3
    v_real_inst = v_adc_inst * 11.0

    acc_t = 0
    for _ in range(4): acc_t += adc_temp_pin.read_u16()
    v_temp_inst = (acc_t / 262140.0) * 3.3
    if 0.05 < v_temp_inst < 3.25:
        r_ntc = R_REF_NTC * ((3.3 - v_temp_inst) / v_temp_inst)
        steinhart = math.log(r_ntc / R0_NTC) / BETA_NTC + (1.0 / T0_KELVIN)
        temp_inst = (1.0 / steinhart) - 273.15
    else:
        temp_inst = temp_filtered if temp_filtered > 0 else 25.0

    i_inst = 0.0
    if ina_available and ina is not None and ina.is_alive:
        try:
            i_inst = ina.read_current()
            # Dùng VBUS độ phân giải cao 1.25 mV/LSB của INA226 nếu phát hiện điện áp bus hợp lệ
            v_bus = ina.read_bus_voltage()
            if v_bus > 0.15:
                v_real_inst = v_bus
        except Exception: pass

    p_inst = v_real_inst * i_inst

    if v_out_filtered == 0.0:
        v_adc_filtered = v_adc_inst
        v_out_filtered = v_real_inst
        temp_filtered  = temp_inst
        i_out_filtered = i_inst
        p_out_filtered = p_inst
    else:
        v_adc_filtered = ALPHA_V    * v_adc_inst + (1.0 - ALPHA_V)    * v_adc_filtered
        v_out_filtered = ALPHA_V    * v_real_inst + (1.0 - ALPHA_V)    * v_out_filtered
        temp_filtered  = ALPHA_TEMP * temp_inst   + (1.0 - ALPHA_TEMP) * temp_filtered
        i_out_filtered = ALPHA_I    * i_inst      + (1.0 - ALPHA_I)    * i_out_filtered
        p_out_filtered = ALPHA_I    * p_inst      + (1.0 - ALPHA_I)    * p_out_filtered

    # NGẮT BẢO VỆ PHẦN CỨNG: QUÁ DÒNG (> 5.0A) & QUÁ NHIỆT (> 75.0°C)
    if current_duty > 0.0:
        if i_out_filtered > MAX_CURRENT_LIMIT:
            current_duty = 0.0
            update_duty(0.0)
            control_mode = "MANUAL"
            if not is_overcurrent_tripped:
                is_overcurrent_tripped = True
                add_log("alarm", "Bảo Vệ", f"NGẮT KHẨN CẤP QUÁ DÒNG ({i_out_filtered:.2f}A > {MAX_CURRENT_LIMIT}A)! Đã ngắt xung PWM.")
        else:
            if i_out_filtered < MAX_CURRENT_LIMIT - 0.5:
                is_overcurrent_tripped = False

        if temp_filtered > MAX_TEMP_LIMIT:
            current_duty = 0.0
            update_duty(0.0)
            control_mode = "MANUAL"
            if not is_overtemp_tripped:
                is_overtemp_tripped = True
                add_log("alarm", "Bảo Vệ", f"NGẮT KHẨN CẤP QUÁ NHIỆT ({temp_filtered:.1f}°C > {MAX_TEMP_LIMIT}°C)! Đã ngắt xung PWM.")
        else:
            if temp_filtered < MAX_TEMP_LIMIT - 5.0:
                is_overtemp_tripped = False

    return (round(v_adc_filtered, 3),
            round(v_out_filtered, 3),
            round(temp_filtered,  1),
            round(i_out_filtered, 3),
            round(p_out_filtered, 2))

telemetry_cache = {
    "d": 0.0, "v_adc": 0.0, "v_out": 0.0, "v_target": 0.0,
    "i_out": 0.0, "p_out": 0.0, "mode": "MANUAL",
    "temp": 25.0, "tuning": False, "tuning_pct": 0, "ina_ok": False,
    "last_user": "Hệ thống", "last_action": "Khởi tạo",
    "tn": 0, "tp": None,
    "max_duty": MAX_DUTY, "max_temp": MAX_TEMP_LIMIT,
    "lut_file": LUT_FILE
}

GRAPH_X_LEFT = 26
GRAPH_WIDTH = 128 - GRAPH_X_LEFT
GRAPH_Y_TOP = 18
GRAPH_Y_BOTTOM = 63
GRAPH_HEIGHT = GRAPH_Y_BOTTOM - GRAPH_Y_TOP
v_history = [0.0] * GRAPH_WIDTH
oled_mode = "VOLT"

def render_oled_graph(v_now, temp_now, buf_pwm, i_now=0.0):
    global oled, oled_available
    if not oled_available or oled is None: return
    try:
        oled.fill(0)
        if oled_mode == "VOLT":
            v_history.append(v_now)
            v_history.pop(0)
            oled.text(f"V:{v_now:4.1f}V I:{i_now:4.2f}A", 0, 0)
            oled.text(f"D:{current_duty:4.2f}% T:{temp_now:2.0f}C", 0, 9)
            oled.hline(0, 17, 128, 1)
            oled.text("15", 8, 18)
            oled.text("7.5", 0, 38)
            oled.text("0", 16, 56)
            oled.vline(25, GRAPH_Y_TOP, GRAPH_HEIGHT + 1, 1)
            oled.hline(23, GRAPH_Y_TOP, 3, 1)
            oled.hline(23, GRAPH_Y_TOP + (GRAPH_HEIGHT // 2), 3, 1)
            oled.hline(23, GRAPH_Y_BOTTOM, 3, 1)
            
            if control_mode == "AUTO_V" and v_target > 0:
                y_tgt = GRAPH_Y_BOTTOM - int((min(15.0, v_target) / 15.0) * GRAPH_HEIGHT)
                if GRAPH_Y_TOP <= y_tgt <= GRAPH_Y_BOTTOM:
                    for x in range(GRAPH_X_LEFT, 128, 4):
                        oled.pixel(x, y_tgt, 1)

            prev_v = min(15.0, max(0.0, v_history[0]))
            prev_y = GRAPH_Y_BOTTOM - int((prev_v / 15.0) * GRAPH_HEIGHT)
            for i in range(1, GRAPH_WIDTH):
                curr_v = min(15.0, max(0.0, v_history[i]))
                curr_y = GRAPH_Y_BOTTOM - int((curr_v / 15.0) * GRAPH_HEIGHT)
                oled.line(GRAPH_X_LEFT + (i - 1), prev_y, GRAPH_X_LEFT + i, curr_y, 1)
                prev_y = curr_y
        oled.show()
    except Exception: pass

current_duty = 0.0
update_duty(current_duty)

control_mode = "MANUAL"
v_target = 0.0
step_size = 0.20
tolerance = 0.05

last_v_out = 0.0
last_control_time = time.ticks_ms()

pno_fault_timer = time.ticks_ms()
pno_ref_v = 0.0
pno_ref_duty = 0.0
pno_stall_cycles = 0

NUM_SAMPLES = 100
buf_gp14 = bytearray(NUM_SAMPLES)
buf_gp15 = bytearray(NUM_SAMPLES)

def trigger_autov_abort(v_now, reason):
    global control_mode, current_duty
    control_mode = "MANUAL"
    current_duty = 0.0
    update_duty(0.0)
    telemetry_cache["mode"] = "MANUAL"
    telemetry_cache["d"] = 0.0
    telemetry_cache["last_action"] = "BẢO VỆ: Dừng bám áp"
    add_log("alarm", "Bảo Vệ", f"NGẮT BÁM ÁP: {reason}! Đã chuyển về Thủ công và đưa Duty về 0%.")
    print(f"[AUTO_V ABORT] {reason}")

def process_pno_regulation(v_now):
    global current_duty, control_mode, pno_fault_timer, pno_ref_v, pno_ref_duty, pno_stall_cycles
    if control_mode != "AUTO_V": return
    if v_target <= 0.01:
        if current_duty != 0.0:
            current_duty = 0.0
            update_duty(0.0)
        return

    err = v_target - v_now

    # Đã bám sát mục tiêu trong dải dung sai cho phép
    if abs(err) <= tolerance:
        pno_fault_timer = time.ticks_ms()
        pno_ref_v = v_now
        pno_ref_duty = current_duty
        pno_stall_cycles = 0
        return

    now_ms = time.ticks_ms()

    # PHÁT HIỆN BẤT THƯỜNG / ĐẦU RA KHÔNG TĂNG (STALL / SHORT / DISCONNECT DETECTION)
    # 1. Duty đã tăng đáng kể (>= 5.0%) nhưng đầu ra vẫn xấp xỉ 0 (< 0.25V)
    if err > 1.0 and current_duty >= 5.0 and v_now < 0.25:
        if time.ticks_diff(now_ms, pno_fault_timer) >= 1200:
            trigger_autov_abort(v_now, f"Đầu ra không có áp (Vout={v_now:.2f}V khi Duty={current_duty:.1f}%)")
            return

    # 2. Đang cần tăng áp (err > 0.5V), P&O tăng Duty nhưng đầu ra hoàn toàn không tăng theo
    if err > 0.5:
        if time.ticks_diff(now_ms, pno_fault_timer) >= 1500:
            if (current_duty - pno_ref_duty >= 1.5 or current_duty >= MAX_DUTY - 0.2) and (v_now - pno_ref_v <= 0.05):
                pno_stall_cycles += 1
                if pno_stall_cycles >= 2: # Bất thường kéo dài ~3 giây
                    trigger_autov_abort(v_now, f"Đầu ra không tăng theo Duty (Vout={v_now:.2f}V, Duty={current_duty:.1f}%)")
                    return
            else:
                pno_stall_cycles = 0

            pno_fault_timer = now_ms
            pno_ref_v = v_now
            pno_ref_duty = current_duty
    else:
        pno_fault_timer = now_ms
        pno_ref_v = v_now
        pno_ref_duty = current_duty
        pno_stall_cycles = 0

    fine_step = max(step_size, 0.25) if abs(err) > 2.0 else 0.10 if abs(err) > 0.3 else 0.05
    if err > 0: current_duty += fine_step
    else: current_duty -= fine_step
    current_duty = max(MIN_DUTY, min(MAX_DUTY, current_duty))
    update_duty(current_duty)

tuning_in_progress = False
tuning_progress_pct = 0
tuning_results = []
tuning_step = 0
total_tuning_samples = 20
tuning_timer = 0
saved_mode = "MANUAL"
tuning_v_limit = 12.0

TUNE_SAMPLE_MS = 45
TUNE_WINDOW_MS = 180
TUNE_SETTLE_TOL = 0.03
TUNE_SETTLE_NEED = 3
TUNE_SETTLE_TIMEOUT = 3600
TUNE_DISCHARGE_MS = 3600
TUNE_POINT_TOL = 0.08
TUNE_MAX_ITER = 4
TUNE_MAX_STEP = 5.0
TUNE_OVER_V = 0.5

tune_phase = "IDLE"
tune_target_v = 0.0
tune_iter = 0
tune_slope = 0.5
tune_prev_d = None
tune_prev_v = None
tune_ref_v = 0.0
tune_ref_t = 0
tune_stable_cnt = 0
tune_phase_t0 = 0
tune_discharged = False
tuning_initiator = "Hệ thống"

grid_5v_15 = [0.8, 1.5, 2.2, 2.7, 3.0, 3.2, 3.3, 3.4, 3.6, 4.0, 4.3, 4.6, 4.8, 4.95, 5.0]

def start_tuning_sweep(vtgt, max_pts=20, initiator="Hệ thống"):
    global current_duty, control_mode, tuning_in_progress, tuning_progress_pct
    global tuning_results, tuning_step, total_tuning_samples, tuning_v_limit
    global tuning_timer, saved_mode, tuning_raw_data, pending_sweep_results
    global tune_phase, tune_slope, tune_prev_d, tune_prev_v, tune_phase_t0
    global tune_discharged, tuning_initiator

    if tuning_in_progress: return

    current_duty = 0.0
    update_duty(0.0)

    total_tuning_samples = max(2, min(100, int(max_pts)))
    tuning_v_limit = max(0.5, min(15.0, float(vtgt)))

    tuning_initiator = initiator
    telemetry_cache["tuning_by"] = initiator
    tune_discharged = False

    tuning_in_progress = True
    tuning_results = []
    tuning_raw_data = []
    pending_sweep_results = []
    tuning_step = 0
    tuning_progress_pct = 0
    saved_mode = control_mode
    control_mode = "TUNING"
    tune_slope = 0.5
    tune_prev_d = None
    tune_prev_v = None

    tune_phase = "DISCHARGE"
    tune_phase_t0 = time.ticks_ms()
    tuning_timer = tune_phase_t0

def stop_tuning_sweep():
    global tuning_in_progress
    if tuning_in_progress:
        tuning_finish("Dừng quét theo yêu cầu người dùng")

def tune_apply_duty(d_new, now):
    global current_duty, tune_phase, tune_phase_t0
    global tune_ref_v, tune_ref_t, tune_stable_cnt
    d_new = max(current_duty - TUNE_MAX_STEP, min(current_duty + TUNE_MAX_STEP, d_new))
    current_duty = max(MIN_DUTY, min(MAX_DUTY, d_new))
    update_duty(current_duty)
    tune_phase = "SETTLE"
    tune_phase_t0 = now
    tune_ref_v = telemetry_cache["v_out"]
    tune_ref_t = now
    tune_stable_cnt = 0

def tune_begin_point(now):
    global tune_target_v, tune_iter
    if abs(tuning_v_limit - 5.0) < 0.2 and total_tuning_samples == 15 and tuning_step < 15:
        tune_target_v = grid_5v_15[tuning_step]
    else:
        tune_target_v = round(tuning_v_limit * (tuning_step + 1) / float(total_tuning_samples), 2)
    tune_iter = 0
    if tune_prev_d is None:
        d_new = tune_target_v / tune_slope * 0.85
    else:
        d_new = tune_prev_d + (tune_target_v - tune_prev_v) / tune_slope * 0.85
    tune_apply_duty(d_new, now)

def tuning_finish(reason="OK"):
    global tuning_in_progress, control_mode, current_duty, tuning_raw_data, tune_phase, pending_sweep_results
    global tuning_progress_pct
    pts = []
    if len(tuning_results) > 0:
        # Thêm điểm neo (0V, 0%) để nội suy tiệm cận gốc
        pts.append({"target": 0.0, "duty": 0.0, "volt": 0.0})
        for p in sorted(tuning_results, key=lambda r: r["volt"]):
            if p["volt"] > pts[-1]["volt"] + 0.01:
                pts.append(p)
        
        # Ép Duty đơn điệu tăng dần (Monotonic increasing)
        for i in range(1, len(pts)):
            if pts[i]["duty"] < pts[i - 1]["duty"]:
                pts[i]["duty"] = pts[i - 1]["duty"]
        
        pending_sweep_results = list(pts)
        add_log("action", "Hệ thống", f"Thu thập xong Auto-Tuning ({len(pts)} mẫu, có điểm neo & ép đơn điệu). Đang đợi đặt tên file...")
    else:
        pending_sweep_results = []
        add_log("alarm", "Hệ thống", f"Dừng Auto-Tuning: Không thu thập được mẫu đo nào ({reason})")

    tune_phase = "IDLE"
    tuning_in_progress = False
    tuning_progress_pct = 100
    telemetry_cache["tuning_pct"] = 100
    telemetry_cache["tuning"] = False
    control_mode = saved_mode
    current_duty = estimate_duty_adaptive(v_target) if (control_mode == "AUTO_V" and v_target > 0) else 0.0
    update_duty(current_duty)

def tuning_tick(now):
    global tune_iter, tune_slope, tune_prev_d, tune_prev_v
    global tune_ref_v, tune_ref_t, tune_stable_cnt
    global tuning_step, tuning_progress_pct, tuning_raw_data, tune_discharged

    v = telemetry_cache["v_out"]

    if tune_phase == "DISCHARGE":
        first_v = tuning_v_limit / float(total_tuning_samples)
        if v <= first_v + TUNE_POINT_TOL or time.ticks_diff(now, tune_phase_t0) >= TUNE_DISCHARGE_MS:
            if v > tuning_v_limit + TUNE_OVER_V:
                tuning_finish("Áp tồn dư quá cao (> V_limit). Vui lòng xả tải trước khi quét!")
                return
            tune_discharged = True
            tune_begin_point(now)
        return

    if tune_discharged and v > tuning_v_limit + TUNE_OVER_V:
        tuning_finish("Vout vượt giới hạn (Hoàn tất phiên quét)")
        return

    if time.ticks_diff(now, tune_ref_t) >= TUNE_WINDOW_MS:
        if abs(v - tune_ref_v) <= TUNE_SETTLE_TOL: tune_stable_cnt += 1
        else: tune_stable_cnt = 0
        tune_ref_v = v
        tune_ref_t = now

    settled = tune_stable_cnt >= TUNE_SETTLE_NEED
    timed_out = time.ticks_diff(now, tune_phase_t0) >= TUNE_SETTLE_TIMEOUT
    if not (settled or timed_out): return

    tune_iter += 1
    err = tune_target_v - v

    if tune_prev_d is not None and abs(current_duty - tune_prev_d) > 0.05:
        slope = (v - tune_prev_v) / (current_duty - tune_prev_d)
        if 0.1 <= slope <= 5.0: tune_slope = slope
    tune_prev_d = current_duty
    tune_prev_v = v

    accepted = abs(err) <= TUNE_POINT_TOL
    exhausted = tune_iter >= TUNE_MAX_ITER
    at_max = current_duty >= MAX_DUTY - 0.01 and err > TUNE_POINT_TOL
    at_zero = current_duty <= 0.0 and err < -TUNE_POINT_TOL

    if not (accepted or exhausted or at_max or at_zero):
        gain = 0.85 if err > 0 else 1.0
        tune_apply_duty(current_duty + gain * err / tune_slope, now)
        return

    if not at_zero:
        tuning_results.append({
            "target": round(tune_target_v, 2),
            "duty": round(current_duty, 2),
            "volt": round(v, 3)
        })
        tuning_raw_data = list(tuning_results)

    tuning_step += 1
    tuning_progress_pct = int(tuning_step * 100 / float(total_tuning_samples))

    if at_max:
        tuning_finish("Đạt Duty tối đa (Hoàn tất quét)")
    elif tuning_step >= total_tuning_samples:
        tuning_finish("OK")
    else:
        tune_begin_point(now)

def execute_command(cmd_str, user_str="User"):
    global control_mode, current_duty, v_target, oled_mode, LUT_FILE, pending_sweep_results
    global MAX_DUTY, MAX_TEMP_LIMIT, pno_fault_timer, pno_ref_v, pno_ref_duty, pno_stall_cycles, last_sse_broadcast
    cmd_str = url_unquote(cmd_str).strip()
    telemetry_cache["last_user"] = user_str
    last_sse_broadcast = 0  # Phát gói tin telemetry mới ngay lập tức (<5ms) sau khi nhận lệnh

    if cmd_str.startswith("SYNC_TIME:"):
        try:
            val_str = cmd_str.split(":", 1)[1]
            set_pico_time(val_str)
        except Exception: pass

    elif cmd_str.startswith("LOGOUT"):
        tok = cmd_str.split(":", 1)[1] if ":" in cmd_str else ""
        if user_str in active_tokens:
            if tok and isinstance(active_tokens[user_str], set):
                active_tokens[user_str].discard(tok)
                if not active_tokens[user_str]:
                    del active_tokens[user_str]
            elif not tok:
                try: del active_tokens[user_str]
                except Exception: pass
        to_close = [s for s, inf in sse_clients.items() if inf.get("user") == user_str and (not tok or inf.get("token") == tok)]
        for s in to_close:
            try: s.close()
            except Exception: pass
            if s in sse_clients:
                del sse_clients[s]
        telemetry_cache["last_action"] = "Đã đăng xuất"
        add_log("login", user_str, f"Tài khoản {user_str} đã chủ động đăng xuất")

    elif cmd_str.startswith("SET:"):
        if tuning_in_progress:
            add_log("alarm", user_str, "Từ chối chỉnh Duty: Đang trong quá trình Auto-Tuning!")
            return
        try:
            val = float(cmd_str.split(":")[1])
            control_mode = "MANUAL"
            current_duty = max(MIN_DUTY, min(MAX_DUTY, val))
            update_duty(current_duty)
            telemetry_cache["d"] = round(current_duty, 2)
            telemetry_cache["mode"] = control_mode
            telemetry_cache["last_action"] = f"Chỉnh Duty = {current_duty:.2f}%"
            add_log("action", user_str, f"Chỉnh Duty Cycle thành {current_duty:.2f}%")
        except Exception: pass

    elif cmd_str.startswith("SET_V:"):
        if tuning_in_progress:
            add_log("alarm", user_str, "Từ chối đặt V_target: Đang trong quá trình Auto-Tuning!")
            return
        try:
            val = float(cmd_str.split(":")[1])
            v_target = max(0.0, min(15.0, val))
            control_mode = "AUTO_V"
            current_duty = estimate_duty_adaptive(v_target)
            update_duty(current_duty)
            telemetry_cache["d"] = round(current_duty, 2)
            telemetry_cache["v_target"] = v_target
            telemetry_cache["mode"] = control_mode
            pno_fault_timer = time.ticks_ms()
            pno_ref_v = telemetry_cache.get("v_out", 0.0)
            pno_ref_duty = current_duty
            pno_stall_cycles = 0
            telemetry_cache["last_action"] = f"Đặt V_target = {v_target:.1f}V"
            add_log("action", user_str, f"Cài đặt V_target = {v_target:.1f}V")
        except Exception: pass

    elif cmd_str.startswith("SET_LIMITS:"):
        try:
            parts = cmd_str.split(":")
            new_t = float(parts[1])
            new_d = float(parts[2])
            if 30.0 <= new_t <= 85.0:
                MAX_TEMP_LIMIT = round(new_t, 1)
            if 5.0 <= new_d <= 45.0:
                MAX_DUTY = round(new_d, 2)
                if current_duty > MAX_DUTY:
                    current_duty = MAX_DUTY
                    update_duty(current_duty)
                    telemetry_cache["d"] = round(current_duty, 2)
            telemetry_cache["max_temp"] = MAX_TEMP_LIMIT
            telemetry_cache["max_duty"] = MAX_DUTY
            telemetry_cache["last_action"] = f"Giới hạn: {MAX_TEMP_LIMIT:.1f}°C / {MAX_DUTY:.1f}%"
            add_log("action", user_str, f"Cập nhật giới hạn: Nhiệt độ max = {MAX_TEMP_LIMIT:.1f}°C, Duty max = {MAX_DUTY:.1f}%")
        except Exception as e:
            print("[SET_LIMITS Err]", e)

    elif cmd_str.startswith("TUNING:"):
        try:
            parts = cmd_str.split(":")
            v_limit = float(parts[1])
            pts_cnt = int(parts[2]) if len(parts) > 2 else 20
            start_tuning_sweep(v_limit, pts_cnt, initiator=user_str)
            telemetry_cache["tuning"] = True
            telemetry_cache["mode"] = "TUNING"
            telemetry_cache["last_action"] = f"Quét Auto-Tuning ({v_limit}V, {pts_cnt} mẫu)"
            add_log("action", user_str, f"Bắt đầu Auto-Tuning ({v_limit}V, {pts_cnt} mẫu)")
        except Exception: pass

    elif cmd_str == "STOP_TUNING":
        stop_tuning_sweep()
        pending_sweep_results = []
        tuning_results = []
        load_20pts_lut()
        telemetry_cache["tuning"] = False
        telemetry_cache["tuning_pct"] = 0
        telemetry_cache["last_action"] = "Đã hủy Auto-Tuning"
        add_log("action", user_str, "Đã yêu cầu dừng quét và hủy dữ liệu quét")

    elif cmd_str.startswith("SAVE_SWEEP_FILE:"):
        fname = url_unquote(cmd_str.split(":", 1)[1]).strip()
        if not fname.endswith(".json"): fname += ".json"
        data_to_save = pending_sweep_results if pending_sweep_results else tuning_results
        if not data_to_save:
            add_log("alarm", user_str, f"Hủy lưu file {fname}: Không có mẫu quét nào!")
            return
        LUT_FILE = fname
        save_20pts_lut(data_to_save)
        telemetry_cache["lut_file"] = LUT_FILE
        telemetry_cache["last_action"] = f"Lưu file LUT = {LUT_FILE}"
        add_log("action", user_str, f"Đã lưu kết quả Auto-Tuning ({len(data_to_save)} mẫu) vào file {LUT_FILE}")

    elif cmd_str.startswith("SET_FILE:"):
        fname = url_unquote(cmd_str.split(":", 1)[1]).strip()
        if fname:
            LUT_FILE = fname
            load_20pts_lut()
            telemetry_cache["lut_file"] = LUT_FILE
            telemetry_cache["last_action"] = f"Đổi file LUT = {LUT_FILE}"
            add_log("action", user_str, f"Kích hoạt file LUT {LUT_FILE}")

server = None
poller = None

def reinit_server_socket():
    global server, poller
    try:
        if poller and server:
            try: poller.unregister(server)
            except Exception: pass
        if server:
            try: server.close()
            except Exception: pass
    except Exception: pass
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", 80))
        server.listen(5)
        server.setblocking(False)
        poller = select.poll()
        poller.register(server, select.POLLIN)
        print("[NET] Server socket khởi tạo sẵn sàng trên cổng 80")
    except Exception as e:
        print("[NET] Khởi tạo server socket lỗi:", e)

reinit_server_socket()

sse_clients = {}

def is_token_valid(u_str, t_str):
    if not u_str: return False
    toks = active_tokens.get(u_str, set())
    if isinstance(toks, str): toks = {toks}
    return t_str in toks

def send_http_json(sock, status_code, status_text, data_dict):
    try:
        body = ujson.dumps(data_dict).encode("utf-8")
        header = f"HTTP/1.1 {status_code} {status_text}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\n\r\n".encode("utf-8")
        sock.settimeout(1.5)
        sock.sendall(header + body)
    except Exception: pass

def send_http_text(sock, status_code, status_text, text_str):
    try:
        body = text_str.encode("utf-8")
        header = f"HTTP/1.1 {status_code} {status_text}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\n\r\n".encode("utf-8")
        sock.settimeout(0.2)
        sock.sendall(header + body)
    except Exception: pass

last_oled_time = time.ticks_ms()
last_sample_time = time.ticks_ms()
last_sse_broadcast = time.ticks_ms()
last_log_sse_sync = time.ticks_ms()

# BẬT WATCHDOG PHẦN CỨNG BẢO VỆ BÀI THI:
# True: Pico W tự khởi động lại sau 8 giây nếu xảy ra bất kỳ sự cố treo cứng phần cứng nào.
# False: Tắt watchdog nếu bạn đang kết nối Thonny IDE để viết / sửa code thử nghiệm.
USE_WDT = False

class DummyWDT:
    def feed(self): pass

try:
    wdt = WDT(timeout=8000) if USE_WDT else DummyWDT()
except Exception:
    wdt = DummyWDT()

def process_http_requests(now):
    global server, poller, LUT_FILE
    if not server or not poller:
        reinit_server_socket()
        return

    try:
        res = poller.poll(0)
    except Exception:
        reinit_server_socket()
        return

    if not res: return
    for sock, event in res:
        if sock == server:
            client_sock = None
            try:
                client_sock, _ = server.accept()
                client_sock.settimeout(0.25)
                try:
                    request = client_sock.recv(1024).decode("utf-8", "ignore")
                except OSError:
                    request = ""
                req_line = request.split("\r\n")[0] if request else ""

                if not req_line:
                    pass
                elif "GET /api/stream" in req_line:
                    params = parse_query_params(req_line)
                    u = params.get("u", "")
                    t = params.get("t", "")
                    
                    # Đóng kết nối cũ cùng token (tránh rò rỉ socket khi F5/reconnect)
                    for s_old, inf in list(sse_clients.items()):
                        if t and inf.get("token") == t:
                            try: s_old.close()
                            except Exception: pass
                            del sse_clients[s_old]

                    # Giữ tối đa 3 kết nối SSE đồng thời để đảm bảo tài nguyên mạng Pico W
                    if len(sse_clients) >= 3:
                        old_sock = list(sse_clients.keys())[0]
                        try: old_sock.close()
                        except Exception: pass
                        del sse_clients[old_sock]

                    sse_header = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\nAccess-Control-Allow-Origin: *\r\n\r\n"
                    client_sock.sendall(sse_header)
                    client_sock.setblocking(False)
                    
                    sse_clients[client_sock] = {"user": u, "token": t, "errs": 0}
                    client_sock = None

                elif "GET / " in req_line or "GET /index.html" in req_line:
                    try:
                        client_sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n")
                        with open("index.html", "rb") as f:
                            while True:
                                chunk = f.read(1024)
                                if not chunk: break
                                client_sock.sendall(chunk)
                    except Exception:
                        client_sock.sendall(b"HTTP/1.1 404 Not Found\r\n\r\nThieu file index.html!")

                elif "GET /manifest.json" in req_line:
                    try:
                        client_sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/manifest+json; charset=utf-8\r\nCache-Control: public, max-age=86400\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n")
                        with open("manifest.json", "rb") as f:
                            while True:
                                chunk = f.read(1024)
                                if not chunk: break
                                client_sock.sendall(chunk)
                    except Exception:
                        client_sock.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")

                elif "GET /sw.js" in req_line:
                    try:
                        client_sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/javascript; charset=utf-8\r\nCache-Control: public, max-age=86400\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n")
                        with open("sw.js", "rb") as f:
                            while True:
                                chunk = f.read(1024)
                                if not chunk: break
                                client_sock.sendall(chunk)
                    except Exception:
                        client_sock.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")

                elif "GET /logo.png" in req_line:
                    try:
                        client_sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nCache-Control: public, max-age=86400\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n")
                        with open("logo.png", "rb") as f:
                            while True:
                                chunk = f.read(1024)
                                if not chunk: break
                                client_sock.sendall(chunk)
                    except Exception:
                        client_sock.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")

                elif "GET /api/login" in req_line:
                    params = parse_query_params(req_line)
                    u = url_unquote(params.get("u", "")).strip()
                    p = url_unquote(params.get("p", "")).strip()

                    valid_pass = False
                    if u in ACCOUNTS:
                        allowed = ACCOUNTS[u] if isinstance(ACCOUNTS[u], list) else [ACCOUNTS[u]]
                        if p in allowed:
                            valid_pass = True

                    if valid_pass:
                        new_token = f"token_{now}_{u}"
                        # Hỗ trợ đa thiết bị đăng nhập đồng thời (tối đa 5 phiên cho mỗi tài khoản)
                        if u not in active_tokens:
                            active_tokens[u] = set()
                        elif isinstance(active_tokens[u], str):
                            active_tokens[u] = {active_tokens[u]}
                        active_tokens[u].add(new_token)
                        if len(active_tokens[u]) > 5:
                            active_tokens[u] = set(list(active_tokens[u])[-5:])
                        add_log("login", u, f"Tài khoản {u} đã đăng nhập hệ thống thành công")
                        send_http_json(client_sock, 200, "OK", {"status": "ok", "user": u, "token": new_token})
                    else:
                        send_http_json(client_sock, 400, "Bad Request", {"status": "error", "msg": "Tài khoản hoặc mật khẩu không đúng!"})

                elif "GET /api/logs" in req_line:
                    gc.collect()
                    send_http_json(client_sock, 200, "OK", {"logs": system_logs[:MAX_LOGS]})

                elif "GET /api/lut" in req_line:
                    params = parse_query_params(req_line)
                    req_f = url_unquote(params.get("f", "")).strip()
                    files_list = get_lut_files_list()
                    if req_f and req_f in files_list:
                        LUT_FILE = req_f
                        load_20pts_lut()
                        telemetry_cache["lut_file"] = LUT_FILE
                        telemetry_cache["last_action"] = f"Đổi file LUT = {LUT_FILE}"
                    elif files_list and (LUT_FILE not in files_list or LUT_FILE.startswith("lut_12")):
                        LUT_FILE = files_list[0]
                        load_20pts_lut()
                        telemetry_cache["lut_file"] = LUT_FILE
                    current_raw = tuning_results if tuning_in_progress else tuning_raw_data
                    payload = {"raw": current_raw, "lut_v": v_grid, "lut_d": d_grid, "file": LUT_FILE, "files": files_list}
                    send_http_json(client_sock, 200, "OK", payload)

                elif "GET /api/read_file" in req_line:
                    params = parse_query_params(req_line)
                    user_who = params.get("u", "")
                    token_who = params.get("t", "")
                    if user_who in active_tokens and not is_token_valid(user_who, token_who):
                        send_http_json(client_sock, 401, "Unauthorized", {"error": "Token không hợp lệ"})
                    else:
                        target_file = url_unquote(params.get("f", LUT_FILE)).strip()
                        try:
                            with open(target_file, "r") as f:
                                f_content = f.read()
                            send_http_text(client_sock, 200, "OK", f_content)
                        except Exception:
                            send_http_json(client_sock, 404, "Not Found", {"error": "File not found"})

                elif "GET /api/delete_file" in req_line:
                    params = parse_query_params(req_line)
                    user_who = params.get("u", "")
                    token_who = params.get("t", "")
                    if user_who in active_tokens and not is_token_valid(user_who, token_who):
                        send_http_json(client_sock, 401, "Unauthorized", {"status": "error", "msg": "Phiên làm việc hết hạn hoặc không hợp lệ!"})
                    elif tuning_in_progress:
                        send_http_json(client_sock, 200, "OK", {"status": "error", "msg": "Đang Auto-Tuning, không thể xóa file!"})
                    else:
                        target_file = url_unquote(params.get("f", "")).strip()
                        if target_file and ("/" not in target_file) and (".." not in target_file) and is_data_file(target_file):
                            try:
                                if target_file in os.listdir():
                                    os.remove(target_file)
                                    files = [f for f in get_lut_files_list() if f != target_file]
                                    if target_file == LUT_FILE:
                                        LUT_FILE = files[0] if files else "tai_5V.json"
                                        load_20pts_lut()
                                    add_log("action", user_who if user_who else "Admin", f"Đã xóa tập tin {target_file}")
                                    send_http_json(client_sock, 200, "OK", {"status": "ok", "msg": f"Đã xóa file {target_file}"})
                                else:
                                    send_http_json(client_sock, 200, "OK", {"status": "error", "msg": "Tệp tin không tồn tại trên bộ nhớ Flash!"})
                            except Exception as err_del:
                                send_http_json(client_sock, 200, "OK", {"status": "error", "msg": f"Lỗi xóa file: {str(err_del)}"})
                        else:
                            send_http_json(client_sock, 200, "OK", {"status": "error", "msg": "Tên tệp không hợp lệ (chỉ xóa được file dữ liệu .json)!"})

                elif "GET /api/telemetry" in req_line or "GET /api/status" in req_line:
                    send_http_json(client_sock, 200, "OK", telemetry_cache)

                elif "GET /api/data_history" in req_line:
                    send_http_json(client_sock, 200, "OK", {"records": measurement_records[:20]})

                elif "GET /api/cmd?c=" in req_line:
                    try:
                        params = parse_query_params(req_line)
                        cmd = params.get("c", "")
                        user_who = params.get("u", "User")
                        token_who = params.get("t", "")
                        if user_who in active_tokens and not is_token_valid(user_who, token_who):
                            send_http_json(client_sock, 401, "Unauthorized", {"status": "error", "msg": "Token không hợp lệ hoặc đã hết hạn!"})
                        else:
                            execute_command(cmd, user_who)
                            send_http_json(client_sock, 200, "OK", {
                                "status": "ok",
                                "d": round(current_duty, 2),
                                "v_target": v_target,
                                "mode": control_mode,
                                "v_out": telemetry_cache.get("v_out", 0.0),
                                "lut_file": LUT_FILE
                            })
                    except Exception:
                        send_http_text(client_sock, 400, "Bad Request", "ERR")
                else:
                    client_sock.sendall(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")

            except Exception: pass
            finally:
                if client_sock:
                    try: client_sock.close()
                    except Exception: pass

try:
    while True:
        wdt.feed()
        now = time.ticks_ms()
        process_http_requests(now)

        if gc.mem_free() < 35000:
            gc.collect()

        if time.ticks_diff(now, last_sample_time) >= 30:
            v_adc, v_real, temp_c, i_out, p_out = read_telemetry()
            telemetry_cache.update({
                "d": round(current_duty, 2), "v_adc": v_adc, "v_out": v_real,
                "v_target": v_target, "i_out": i_out, "p_out": p_out,
                "mode": control_mode, "temp": temp_c,
                "tuning": tuning_in_progress, "tuning_pct": tuning_progress_pct,
                "tn": len(tuning_results),
                "tp": tuning_results[-1] if (tuning_in_progress and tuning_results) else None,
                "ina_ok": (ina_available and ina is not None and ina.is_alive)
            })
            last_sample_time = now

        if time.ticks_diff(now, last_record_time) >= 2000:
            last_record_time = now
            add_measurement_record()

        if logs_dirty and time.ticks_diff(now, last_log_save) >= LOG_SAVE_INTERVAL_MS:
            last_log_save = now
            save_logs()

        if tuning_in_progress:
            if time.ticks_diff(now, tuning_timer) >= TUNE_SAMPLE_MS:
                tuning_timer = now
                tuning_tick(now)

        elif time.ticks_diff(now, last_control_time) >= 80:
            v_real = telemetry_cache["v_out"]
            process_pno_regulation(v_real)
            last_v_out = v_real
            last_control_time = now

        if time.ticks_diff(now, last_oled_time) >= (600 if tuning_in_progress else 350):
            if oled_available:
                capture_highspeed(buf_gp14, buf_gp15, NUM_SAMPLES)
                render_oled_graph(telemetry_cache["v_out"], telemetry_cache["temp"], buf_gp14, i_now=telemetry_cache["i_out"])
            last_oled_time = now
            process_http_requests(now)

        if time.ticks_diff(now, last_sse_broadcast) >= 180:
            last_sse_broadcast = now
            if sse_clients:
                capture_highspeed(buf_gp14, buf_gp15, NUM_SAMPLES)

                resp_payload = dict(telemetry_cache)
                resp_payload["log_seq"] = log_seq
                resp_payload["pwma"] = list(buf_gp14[:32])
                resp_payload["pwmb"] = list(buf_gp15[:32])
                
                online_set = set()
                for inf in sse_clients.values():
                    u_name = inf.get("user")
                    if u_name: online_set.add(u_name)
                resp_payload["online_users"] = list(online_set)
                
                if latest_rec_push:
                    resp_payload["latest_rec"] = latest_rec_push
                    latest_rec_push = None

                if latest_log_push:
                    resp_payload["latest_log"] = latest_log_push
                    latest_log_push = None

                json_str = ujson.dumps(resp_payload)
                sse_bytes = ("data: " + json_str + "\n\n").encode("utf-8")
                
                closed_socks = []
                for client_s, info in list(sse_clients.items()):
                    u_user = info.get("user", "")
                    u_token = info.get("token", "")

                    user_tokens = active_tokens.get(u_user, set())
                    if isinstance(user_tokens, str):
                        user_tokens = {user_tokens}

                    if u_token not in user_tokens:
                        try:
                            client_s.sendall(b"event: kickout\ndata: {}\n\n")
                            client_s.close()
                        except Exception: pass
                        closed_socks.append((client_s, u_user, "kicked"))
                    else:
                        try:
                            sent = client_s.send(sse_bytes)
                            if sent > 0:
                                info["errs"] = 0
                            else:
                                cur_errs = info.get("errs", 0) + 1
                                info["errs"] = cur_errs
                                if cur_errs > 3:
                                    try: client_s.close()
                                    except Exception: pass
                                    closed_socks.append((client_s, u_user, "disconnect"))
                        except OSError as e:
                            err_code = e.args[0] if e.args else None
                            # 11: EAGAIN/EWOULDBLOCK, 12: ENOMEM, 110: ETIMEDOUT, 115: EINPROGRESS
                            if err_code == 12:
                                # lwIP cạn kiệt bộ nhớ gói tin -> Hủy ngay kết nối này để giải phóng PCB
                                try: client_s.close()
                                except Exception: pass
                                closed_socks.append((client_s, u_user, "enomem"))
                                gc.collect()
                            elif err_code in (11, 110, 115):
                                cur_errs = info.get("errs", 0) + 1
                                info["errs"] = cur_errs
                                if cur_errs > 3:
                                    try: client_s.close()
                                    except Exception: pass
                                    closed_socks.append((client_s, u_user, "disconnect"))
                            else:
                                try: client_s.close()
                                except Exception: pass
                                closed_socks.append((client_s, u_user, "disconnect"))
                        except Exception:
                            try: client_s.close()
                            except Exception: pass
                            closed_socks.append((client_s, u_user, "disconnect"))

                for cs, u_user, reason in closed_socks:
                    if cs in sse_clients:
                        del sse_clients[cs]
                    if u_user:
                        other_open = any(info.get("user") == u_user for info in sse_clients.values())
                        if not other_open and reason == "disconnect":
                            add_log("alarm", u_user, f"Tài khoản {u_user} mất kết nối / rời phiên")

        process_http_requests(now)

except KeyboardInterrupt:
    print("\n[STOP] Nhận lệnh dừng từ Thonny IDE. Đang ngắt hoàn toàn hệ thống...")
    try: sm.active(0)
    except Exception: pass
    try:
        Pin(14, Pin.OUT).value(0)
        Pin(15, Pin.OUT).value(0)
    except Exception: pass
    try: ap.active(False)
    except Exception: pass
    print("[SUCCESS] Đã tắt xung PWM và ngắt phát Wi-Fi. Pico W ở trạng thái nghỉ hoàn toàn.")
    if USE_WDT:
        print("[WDT] Watchdog đang bật, Pico W sẽ tự khởi động lại sau 8 giây.")

except Exception as err:
    print("[CRITICAL ERROR] Lỗi chương trình:", err)
    if USE_WDT:
        time.sleep(1)
        reset()
    reset()