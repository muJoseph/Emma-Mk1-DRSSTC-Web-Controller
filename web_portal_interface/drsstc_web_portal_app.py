import datetime
import json
import os

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

app = Flask(__name__)

# Default start-burst form values when no persisted file exists (must match controller defaults)
DEFAULT_START_FREQ_HZ = 264000
DEFAULT_PRF_PERIOD_US = 5000
DEFAULT_PRF_ONTIME_US = 100
DEFAULT_LEAD_TIME_NS = 0
DEFAULT_NUM_BURST_CYCLES = 1
# Match firmware mujoe_resdrv start-freq tracking window defaults
DEFAULT_START_FREQ_MAX_NEG_DEV_PERCNT = 25
DEFAULT_START_FREQ_MAX_POS_DEV_PERCNT = 5
START_FREQ_MAX_NEG_DEV_PERCNT_MIN = 0
START_FREQ_MAX_NEG_DEV_PERCNT_MAX = 100
START_FREQ_MAX_POS_DEV_PERCNT_MIN = 0
START_FREQ_MAX_POS_DEV_PERCNT_MAX = 100

# Timed burst sequence form defaults (must match controller defaults)
DEFAULT_SEQUENCE_DURATION_MS = 1000
SEQUENCE_DURATION_MS_MIN = 1
SEQUENCE_DURATION_MS_MAX = 10_000

# Lead time bounds for burst/timed-burst/set-lead-time (must match controller validation)
LEAD_TIME_NS_MIN = 0
LEAD_TIME_NS_MAX = 800

# Set OCD params form defaults and bounds (must match controller validation)
OCD_MAX_CURRENT_A_MIN = 50
OCD_MAX_CURRENT_A_MAX = 800
DEFAULT_OCD_MAX_CURRENT_A = 100
DEFAULT_INPUT_HYSTERESIS = 5  # 50 mV (CompHysteresis.HYSTERESIS_50MV)

# Dropdown choices aligned with MujoeSerialCommRpi.SetOCDParamsCommandArgs.CompHysteresis
OCD_HYSTERESIS_CHOICES = [
    {"value": 0, "label": "No hysteresis (0 mV)"},
    {"value": 1, "label": "10 mV"},
    {"value": 2, "label": "20 mV"},
    {"value": 3, "label": "30 mV"},
    {"value": 4, "label": "40 mV"},
    {"value": 5, "label": "50 mV"},
    {"value": 6, "label": "60 mV"},
    {"value": 7, "label": "70 mV"},
]

# Default CSV path on the controller host for resonant period capture dumps
DEFAULT_RES_PER_CAP_CSV_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "res_per_cap_buffer.csv"
)


# Lead time slider step (Set lead time block)
LEAD_TIME_SLIDER_STEP = 25


def _format_khz(hz: int) -> str:
    """kHz the operator sees. Trailing zeros dropped so 264000 Hz becomes 264."""
    text = f"{hz / 1000:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _res_per_cap_download_name() -> str:
    """Browser save name from the last successful burst and the local click time."""
    params = _get_last_start_burst_params()
    if params is None:
        params = {
            "start_freq_hz": DEFAULT_START_FREQ_HZ,
            "prf_period_us": DEFAULT_PRF_PERIOD_US,
            "prf_ontime_us": DEFAULT_PRF_ONTIME_US,
            "lead_time_ns": DEFAULT_LEAD_TIME_NS,
            "num_burst_cycles": DEFAULT_NUM_BURST_CYCLES,
            "start_freq_max_neg_dev_percnt": DEFAULT_START_FREQ_MAX_NEG_DEV_PERCNT,
            "start_freq_max_pos_dev_percnt": DEFAULT_START_FREQ_MAX_POS_DEV_PERCNT,
        }
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        f"f_ol_{_format_khz(params['start_freq_hz'])}"
        f"_prf_per_{params['prf_period_us']}"
        f"_prf_on_{params['prf_ontime_us']}"
        f"_lead_{params['lead_time_ns']}"
        f"_cyc_{params['num_burst_cycles']}"
        f"_fneg_{params['start_freq_max_neg_dev_percnt']}"
        f"_fpos_{params['start_freq_max_pos_dev_percnt']}"
        f"_{stamp}.csv"
    )


def _get_last_start_burst_params():
    """Read last successful start-burst params from JSON file. Returns dict or None if missing/invalid."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo_root, "web_portal_interface", "data", "last_start_burst_params.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        required = ("start_freq_hz", "prf_period_us", "prf_ontime_us", "lead_time_ns")
        if not all(k in data for k in required):
            return None
        # Older JSON may omit num_burst_cycles; treat as default 1
        num_burst_cycles = int(data["num_burst_cycles"]) if "num_burst_cycles" in data else DEFAULT_NUM_BURST_CYCLES
        # Older JSON may omit start-freq deviation percents; use firmware defaults
        start_freq_max_neg_dev_percnt = (
            int(data["start_freq_max_neg_dev_percnt"])
            if "start_freq_max_neg_dev_percnt" in data
            else DEFAULT_START_FREQ_MAX_NEG_DEV_PERCNT
        )
        start_freq_max_pos_dev_percnt = (
            int(data["start_freq_max_pos_dev_percnt"])
            if "start_freq_max_pos_dev_percnt" in data
            else DEFAULT_START_FREQ_MAX_POS_DEV_PERCNT
        )
        return {
            "start_freq_hz": int(data["start_freq_hz"]),
            "prf_period_us": int(data["prf_period_us"]),
            "prf_ontime_us": int(data["prf_ontime_us"]),
            "lead_time_ns": int(data["lead_time_ns"]),
            "num_burst_cycles": num_burst_cycles,
            "start_freq_max_neg_dev_percnt": start_freq_max_neg_dev_percnt,
            "start_freq_max_pos_dev_percnt": start_freq_max_pos_dev_percnt,
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _get_last_timed_burst_sequence_params():
    """Read last successful timed-burst-sequence params from JSON. Returns dict or None."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(
        repo_root,
        "web_portal_interface",
        "data",
        "last_timed_burst_sequence_params.json",
    )
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        required = (
            "start_freq_hz",
            "prf_period_us",
            "prf_ontime_us",
            "lead_time_ns",
            "sequence_duration_ms",
        )
        if not all(k in data for k in required):
            return None
        return {
            "start_freq_hz": int(data["start_freq_hz"]),
            "prf_period_us": int(data["prf_period_us"]),
            "prf_ontime_us": int(data["prf_ontime_us"]),
            "lead_time_ns": int(data["lead_time_ns"]),
            "sequence_duration_ms": int(data["sequence_duration_ms"]),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _hv_bus_state_path():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "web_portal_interface", "data", "last_hv_bus_state.json")


def _get_hv_bus_on():
    """Last successful HV command. Missing file means off; this is not a hardware readback."""
    path = _hv_bus_state_path()
    if not os.path.isfile(path):
        return False
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get("hv_bus_on") is True
    except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return False


def _get_res_per_cap_progress():
    """UART dump progress written by the controller. Missing/invalid file means idle."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo_root, "web_portal_interface", "data", "res_per_cap_progress.json")
    if not os.path.isfile(path):
        return {"state": "idle", "seq": 0, "received": 0, "total": 0, "detail": ""}
    try:
        with open(path) as f:
            data = json.load(f)
        return {
            "state": str(data.get("state", "idle")),
            "seq": int(data.get("seq", 0)),
            "received": int(data.get("received", 0)),
            "total": int(data.get("total", 0)),
            "detail": str(data.get("detail", "")),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return {"state": "idle", "seq": 0, "received": 0, "total": 0, "detail": ""}


def _get_last_command_response():
    """Read last command response from JSON file. Returns dict or None if missing/invalid."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(
        repo_root, "web_portal_interface", "data", "last_command_response.json"
    )
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        required = ("command", "response_type", "response_detail", "timestamp_iso")
        if not all(k in data for k in required):
            return None
        return {
            "command": str(data["command"]),
            "response_type": str(data["response_type"]),
            "response_detail": str(data["response_detail"]),
            "timestamp_iso": str(data["timestamp_iso"]),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _get_last_ocd_params():
    """Read last successful set-OCD params from JSON file. Returns dict or None if missing/invalid."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo_root, "web_portal_interface", "data", "last_ocd_params.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        required = ("ocd_max_current_a", "input_hysteresis")
        if not all(k in data for k in required):
            return None
        ocd_max = int(data["ocd_max_current_a"])
        hyst = int(data["input_hysteresis"])
        if ocd_max < OCD_MAX_CURRENT_A_MIN or ocd_max > OCD_MAX_CURRENT_A_MAX:
            return None
        if hyst < 0 or hyst > 7:
            return None
        return {"ocd_max_current_a": ocd_max, "input_hysteresis": hyst}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _form_values_for_template():
    """Return dict of start-burst form values (saved or defaults) for the index template."""
    params = _get_last_start_burst_params()
    if params is not None:
        return params
    return {
        "start_freq_hz": DEFAULT_START_FREQ_HZ,
        "prf_period_us": DEFAULT_PRF_PERIOD_US,
        "prf_ontime_us": DEFAULT_PRF_ONTIME_US,
        "lead_time_ns": DEFAULT_LEAD_TIME_NS,
        "num_burst_cycles": DEFAULT_NUM_BURST_CYCLES,
        "start_freq_max_neg_dev_percnt": DEFAULT_START_FREQ_MAX_NEG_DEV_PERCNT,
        "start_freq_max_pos_dev_percnt": DEFAULT_START_FREQ_MAX_POS_DEV_PERCNT,
    }


def _timed_burst_form_values_for_template():
    """Return dict of timed-burst form values (saved or defaults) for the index template."""
    params = _get_last_timed_burst_sequence_params()
    if params is not None:
        return {
            "tbs_start_freq_hz": params["start_freq_hz"],
            "tbs_prf_period_us": params["prf_period_us"],
            "tbs_prf_ontime_us": params["prf_ontime_us"],
            "tbs_lead_time_ns": params["lead_time_ns"],
            "tbs_sequence_duration_ms": params["sequence_duration_ms"],
        }
    return {
        "tbs_start_freq_hz": DEFAULT_START_FREQ_HZ,
        "tbs_prf_period_us": DEFAULT_PRF_PERIOD_US,
        "tbs_prf_ontime_us": DEFAULT_PRF_ONTIME_US,
        "tbs_lead_time_ns": DEFAULT_LEAD_TIME_NS,
        "tbs_sequence_duration_ms": DEFAULT_SEQUENCE_DURATION_MS,
    }


def _ocd_form_values_for_template():
    """Return dict of OCD form values (saved or defaults) for the index template."""
    params = _get_last_ocd_params()
    if params is not None:
        return params
    return {
        "ocd_max_current_a": DEFAULT_OCD_MAX_CURRENT_A,
        "input_hysteresis": DEFAULT_INPUT_HYSTERESIS,
    }


@app.route('/')
def index():
    # Render the main page template with saved or default form values and last command response
    burst = _form_values_for_template()
    return render_template(
        "index.html",
        last_command_response=_get_last_command_response(),
        lead_time_ns_min=LEAD_TIME_NS_MIN,
        lead_time_ns_max=LEAD_TIME_NS_MAX,
        lead_time_slider_step=LEAD_TIME_SLIDER_STEP,
        start_freq_max_neg_dev_percnt_min=START_FREQ_MAX_NEG_DEV_PERCNT_MIN,
        start_freq_max_neg_dev_percnt_max=START_FREQ_MAX_NEG_DEV_PERCNT_MAX,
        start_freq_max_pos_dev_percnt_min=START_FREQ_MAX_POS_DEV_PERCNT_MIN,
        start_freq_max_pos_dev_percnt_max=START_FREQ_MAX_POS_DEV_PERCNT_MAX,
        sequence_duration_ms_min=SEQUENCE_DURATION_MS_MIN,
        sequence_duration_ms_max=SEQUENCE_DURATION_MS_MAX,
        ocd_max_current_a_min=OCD_MAX_CURRENT_A_MIN,
        ocd_max_current_a_max=OCD_MAX_CURRENT_A_MAX,
        ocd_hysteresis_choices=OCD_HYSTERESIS_CHOICES,
        res_per_cap_csv_path=DEFAULT_RES_PER_CAP_CSV_PATH,
        res_per_cap_csv_ready=os.path.isfile(DEFAULT_RES_PER_CAP_CSV_PATH),
        hv_bus_on=_get_hv_bus_on(),
        res_per_cap_progress_seq=_get_res_per_cap_progress()["seq"],
        start_freq_khz_display=_format_khz(burst["start_freq_hz"]),
        **burst,
        **_timed_burst_form_values_for_template(),
        **_ocd_form_values_for_template(),
    )


@app.route('/api/last_command_response')
def api_last_command_response():
    """Return last command response as JSON for polling, plus last confirmed HV bus state."""
    payload = _get_last_command_response() or {}
    # Same flag the switch renders from, so a failed command can snap the toggle back
    payload["hv_bus_on"] = _get_hv_bus_on()
    return jsonify(payload)


@app.route('/api/res_per_cap_progress')
def api_res_per_cap_progress():
    """Polled every 250 ms by the dump panel while a transfer is active."""
    return jsonify(_get_res_per_cap_progress())


@app.route('/api/download_res_per_cap_csv')
def api_download_res_per_cap_csv():
    """Browser Save-As for the last resonant period capture CSV written on the host."""
    if not os.path.isfile(DEFAULT_RES_PER_CAP_CSV_PATH):
        return jsonify({"error": "No capture CSV available yet"}), 404
    return send_file(
        DEFAULT_RES_PER_CAP_CSV_PATH,
        mimetype="text/csv",
        as_attachment=True,
        download_name=_res_per_cap_download_name(),
    )


@app.route('/<device>/<action>')
def perform_action(device, action):
    if device == 'drsstc':
        if action == 'start_burst':
            print("Start burst button pressed!")
            message = "Burst is started"
        elif action == 'stop_burst':
            print("Stop burst button pressed!")
            message = "Burst is stopped"
        elif action == 'set_lead_time':
            message = "Lead time update requested"
        elif action == 'set_ocd_params':
            message = "Set OCD parameters requested"
        elif action == 'start_timed_burst_sequence':
            message = "Timed burst sequence start requested"
        elif action == 'get_res_per_cap_buffer':
            message = "Get resonant period capture buffer requested"
        else:
            message = "Invalid action"
    else:
        message = "Invalid device"

    # Optional: emit button press as OSAL message when running under OSAL wrapper
    emit_cb = app.config.get('web_portal_emit')
    emit_ids = app.config.get('web_portal_emit_ids')
    if emit_cb is not None and emit_ids is not None:
        data = {'device': device, 'action': action}
        if device == 'drsstc' and action == 'start_burst':
            for key in (
                'start_freq_hz',
                'prf_period_us',
                'prf_ontime_us',
                'lead_time_ns',
                'num_burst_cycles',
                'start_freq_max_neg_dev_percnt',
                'start_freq_max_pos_dev_percnt',
            ):
                val = request.args.get(key)
                if val is not None:
                    data[key] = val
            emit_cb(emit_ids['start_burst'], data)
        elif device == 'drsstc' and action == 'start_timed_burst_sequence':
            for key in (
                'start_freq_hz',
                'prf_period_us',
                'prf_ontime_us',
                'lead_time_ns',
                'sequence_duration_ms',
            ):
                val = request.args.get(key)
                if val is not None:
                    data[key] = val
            emit_cb(emit_ids['start_timed_burst_sequence'], data)
        elif device == 'drsstc' and action == 'stop_burst':
            emit_cb(emit_ids['stop_burst'], data)
        elif device == 'drsstc' and action == 'power_on_hv_bus':
            emit_cb(emit_ids['power_on_hv_bus'], data)
        elif device == 'drsstc' and action == 'power_off_hv_bus':
            emit_cb(emit_ids['power_off_hv_bus'], data)
        elif device == 'drsstc' and action == 'set_lead_time':
            val = request.args.get('lead_time_ns')
            if val is not None:
                data['lead_time_ns'] = val
            emit_cb(emit_ids['set_lead_time'], data)
        elif device == 'drsstc' and action == 'set_ocd_params':
            for key in ('ocd_max_current_a', 'input_hysteresis'):
                val = request.args.get(key)
                if val is not None:
                    data[key] = val
            emit_cb(emit_ids['set_ocd_params'], data)
        elif device == 'drsstc' and action == 'get_res_per_cap_buffer':
            # Path on the RPi host where the assembled uint32 buffer is written as CSV
            val = request.args.get('save_path')
            if val is not None and str(val).strip() != "":
                data['save_path'] = val
            else:
                data['save_path'] = DEFAULT_RES_PER_CAP_CSV_PATH
            emit_cb(emit_ids['get_res_per_cap_buffer'], data)
        else:
            emit_cb(emit_ids['invalid'], data)

    return redirect(url_for("index"))


if __name__ == '__main__':
    # Run the app on the local network (host='0.0.0.0') for access from other devices
    app.run(debug=True, host='0.0.0.0', port=5000)
