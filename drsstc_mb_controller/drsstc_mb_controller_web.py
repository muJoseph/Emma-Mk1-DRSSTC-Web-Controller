"""
OSAL-based controller for MujoeSerialCommOsal driven by the web portal OSAL task.
Builds the logger, OsalTimerProcess, DrsstcWebPortalAppOsal, and MujoeSerialCommOsal.
The main loop reads the web portal emit queue and forwards start burst, stop burst,
timed burst, HV bus power, lead time, OCD parameters, and resonant-period capture
to MujoeSerialCommOsal.
"""
from enum import Enum
import sys
import os

# Script directory is sys.path[0]; put the repo root first so osal, mujoe_ser_comm, and web_portal_interface import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import json
import queue
import time
from typing import Dict, List, Optional, Tuple

import multiprocessing as mp
import numpy as np

from osal.osal_process import OsalTimerProcess
from logging_interface.logger import TerminalLogger
from osal.osal_types import OsalEmitMessage

from mujoe_ser_comm.mujoe_serial_comm_osal import (
    MujoeSerialCommOsal,
    MujoeSerialCommOsalConfig,
    MujoeSerialCommEmitMessageIDs,
)
from mujoe_ser_comm.mujoe_serial_comm_rpi import MujoeSerialCommRpi, RPI_SERIAL_PORTS
from web_portal_interface.drsstc_web_portal_app_osal import (
    DrsstcWebPortalAppOsal,
    DrsstcWebPortalAppOsalConfig,
    WebPortalEmitMessageIDs,
)


################################################################
# Emit queue helper
################################################################


def consume_emit_queue(q: mp.Queue) -> List[OsalEmitMessage]:
    msgs: List[OsalEmitMessage] = []
    while True:
        try:
            msg = q.get(block=False)
            if msg is not None:
                msgs.append(msg)
        except queue.Empty:
            break
    return msgs


################################################################
# Message observer
################################################################


class MessageAwaiterId(Enum):
    SerialComm = 0


class MessageObserver(object):
    def __init__(self):
        self._awaitingSerialCommCmdRsp = False
        self._msgAwaiters: Dict[MessageAwaiterId, Dict[str, List[str]]] = {}
        self._msgAwaiters[MessageAwaiterId.SerialComm] = {"cmd_rsp": []}

    def IsAwaitingMessage(self) -> bool:
        return self._awaitingSerialCommCmdRsp

    def awaitSerialCommCmdRsp(self, message_id: str):
        self._awaitingSerialCommCmdRsp = True
        self._msgAwaiters[MessageAwaiterId.SerialComm]["cmd_rsp"].append(message_id)

    def onSerialCommCmdRsp(self, message_id: str):
        # Check if the message is awaited
        if message_id in self._msgAwaiters[MessageAwaiterId.SerialComm]["cmd_rsp"]:
            # Remove the message from the awaited list
            self._msgAwaiters[MessageAwaiterId.SerialComm]["cmd_rsp"].remove(message_id)
            # If the awaited list is empty, set the awaiting flag to False
            if len(self._msgAwaiters[MessageAwaiterId.SerialComm]["cmd_rsp"]) == 0:
                self._awaitingSerialCommCmdRsp = False


################################################################
# Start-burst parameter bounds and validation
################################################################

UINT32_MAX = 0xFFFFFFFF
START_FREQ_HZ_MIN = 1
START_FREQ_HZ_MAX = 10_000_000
PRF_PERIOD_US_MIN = 1
PRF_PERIOD_US_MAX = 1_000_000
PRF_ONTIME_US_MIN = 1
PRF_ONTIME_US_MAX = 1_000_000  # also constrained by prf_period_us
LEAD_TIME_NS_MIN = 0
LEAD_TIME_NS_MAX = 800
SEQUENCE_DURATION_MS_MIN = 1
SEQUENCE_DURATION_MS_MAX = 10_000  # 10 seconds per firmware/user spec
NUM_BURST_CYCLES_MIN = 0  # 0 = infinite cycles (firmware convention)
# Firmware field is uint32. Finite bursts are limited by duration, not a cycle cap.
NUM_BURST_CYCLES_MAX = UINT32_MAX
BURST_DURATION_MS_MAX = 60_000  # 1 minute; 0 cycles is exempt (runs until stop)
START_FREQ_MAX_NEG_DEV_PERCNT_MIN = 0
START_FREQ_MAX_NEG_DEV_PERCNT_MAX = 100
START_FREQ_MAX_POS_DEV_PERCNT_MIN = 0
START_FREQ_MAX_POS_DEV_PERCNT_MAX = 100
OCD_MAX_CURRENT_A_MIN = 50
OCD_MAX_CURRENT_A_MAX = 800    
OCD_INPUT_HYSTERESIS_MIN = 0
OCD_INPUT_HYSTERESIS_MAX = 7


def _parse_and_validate_start_burst_params(
    data: Optional[Dict],
    default_start_freq_hz: int,
    default_prf_period_us: int,
    default_prf_ontime_us: int,
    default_lead_time_ns: int,
    default_num_burst_cycles: int,
    default_start_freq_max_neg_dev_percnt: int,
    default_start_freq_max_pos_dev_percnt: int,
) -> Tuple[bool, Optional[MujoeSerialCommRpi.StartBurstCommandArgs], str]:
    """Parse and validate start-burst params from web payload. Returns (ok, args, error_msg)."""
    if data is None:
        data = {}
    data = dict(data)

    def parse_uint32(key: str, default: int, lo: int, hi: int) -> Tuple[bool, Optional[int], str]:
        raw = data.get(key)
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            val = default
        else:
            try:
                val = int(float(raw))
            except (TypeError, ValueError):
                return False, None, f"{key}: not a valid number"
            if val < lo or val > hi:
                return False, None, f"{key}: out of range [{lo}, {hi}]"
            if val < 0 or val > UINT32_MAX:
                return False, None, f"{key}: must be in uint32 range"
        return True, val, ""

    ok, start_freq_hz, err = parse_uint32(
        "start_freq_hz", default_start_freq_hz, START_FREQ_HZ_MIN, START_FREQ_HZ_MAX
    )
    if not ok:
        return False, None, err
    ok, prf_period_us, err = parse_uint32(
        "prf_period_us", default_prf_period_us, PRF_PERIOD_US_MIN, PRF_PERIOD_US_MAX
    )
    if not ok:
        return False, None, err
    ok, prf_ontime_us, err = parse_uint32(
        "prf_ontime_us", default_prf_ontime_us, PRF_ONTIME_US_MIN, PRF_ONTIME_US_MAX
    )
    if not ok:
        return False, None, err
    if prf_ontime_us > prf_period_us:
        return False, None, "prf_ontime_us must be <= prf_period_us"
    ok, lead_time_ns, err = parse_uint32(
        "lead_time_ns", default_lead_time_ns, LEAD_TIME_NS_MIN, LEAD_TIME_NS_MAX
    )
    if not ok:
        return False, None, err
    ok, num_burst_cycles, err = parse_uint32(
        "num_burst_cycles",
        default_num_burst_cycles,
        NUM_BURST_CYCLES_MIN,
        NUM_BURST_CYCLES_MAX,
    )
    if not ok:
        return False, None, err
    # 0 cycles is infinite (until Stop Burst). A finite burst must finish within one minute.
    if num_burst_cycles != 0 and prf_period_us * num_burst_cycles > BURST_DURATION_MS_MAX * 1000:
        return False, None, (
            f"burst duration: out of range (period_us * cycles / 1000 must be <= {BURST_DURATION_MS_MAX} ms)"
        )
    # uint8 percent bounds for frequency-tracking window around start freq
    ok, start_freq_max_neg_dev_percnt, err = parse_uint32(
        "start_freq_max_neg_dev_percnt",
        default_start_freq_max_neg_dev_percnt,
        START_FREQ_MAX_NEG_DEV_PERCNT_MIN,
        START_FREQ_MAX_NEG_DEV_PERCNT_MAX,
    )
    if not ok:
        return False, None, err
    ok, start_freq_max_pos_dev_percnt, err = parse_uint32(
        "start_freq_max_pos_dev_percnt",
        default_start_freq_max_pos_dev_percnt,
        START_FREQ_MAX_POS_DEV_PERCNT_MIN,
        START_FREQ_MAX_POS_DEV_PERCNT_MAX,
    )
    if not ok:
        return False, None, err

    args = MujoeSerialCommRpi.StartBurstCommandArgs(
        startFrequency_Hz=np.uint32(start_freq_hz),
        prfPeriod_us=np.uint32(prf_period_us),
        prfOntime_us=np.uint32(prf_ontime_us),
        leadTime_ns=np.uint32(lead_time_ns),
        numBurstCycles=np.uint32(num_burst_cycles),
        startFreqMaxNegDev_percnt=np.uint8(start_freq_max_neg_dev_percnt),
        startFreqMaxPosDev_percnt=np.uint8(start_freq_max_pos_dev_percnt),
    )

    return True, args, ""


def _parse_and_validate_timed_burst_sequence_params(
    data: Optional[Dict],
    default_start_freq_hz: int,
    default_prf_period_us: int,
    default_prf_ontime_us: int,
    default_lead_time_ns: int,
    default_sequence_duration_ms: int,
) -> Tuple[bool, Optional[MujoeSerialCommRpi.StartTimedBurstSequenceCommandArgs], str]:
    """Parse and validate timed-burst-sequence params from web payload."""
    if data is None:
        data = {}
    data = dict(data)

    def parse_uint32(key: str, default: int, lo: int, hi: int) -> Tuple[bool, Optional[int], str]:
        raw = data.get(key)
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            val = default
        else:
            try:
                val = int(float(raw))
            except (TypeError, ValueError):
                return False, None, f"{key}: not a valid number"
            if val < lo or val > hi:
                return False, None, f"{key}: out of range [{lo}, {hi}]"
            if val < 0 or val > UINT32_MAX:
                return False, None, f"{key}: must be in uint32 range"
        return True, val, ""

    ok, start_freq_hz, err = parse_uint32(
        "start_freq_hz", default_start_freq_hz, START_FREQ_HZ_MIN, START_FREQ_HZ_MAX
    )
    if not ok:
        return False, None, err
    ok, prf_period_us, err = parse_uint32(
        "prf_period_us", default_prf_period_us, PRF_PERIOD_US_MIN, PRF_PERIOD_US_MAX
    )
    if not ok:
        return False, None, err
    ok, prf_ontime_us, err = parse_uint32(
        "prf_ontime_us", default_prf_ontime_us, PRF_ONTIME_US_MIN, PRF_ONTIME_US_MAX
    )
    if not ok:
        return False, None, err
    if prf_ontime_us > prf_period_us:
        return False, None, "prf_ontime_us must be <= prf_period_us"
    ok, lead_time_ns, err = parse_uint32(
        "lead_time_ns", default_lead_time_ns, LEAD_TIME_NS_MIN, LEAD_TIME_NS_MAX
    )
    if not ok:
        return False, None, err
    ok, sequence_duration_ms, err = parse_uint32(
        "sequence_duration_ms",
        default_sequence_duration_ms,
        SEQUENCE_DURATION_MS_MIN,
        SEQUENCE_DURATION_MS_MAX,
    )
    if not ok:
        return False, None, err

    args = MujoeSerialCommRpi.StartTimedBurstSequenceCommandArgs(
        startFrequency_Hz=np.uint32(start_freq_hz),
        prfPeriod_us=np.uint32(prf_period_us),
        prfOntime_us=np.uint32(prf_ontime_us),
        leadTime_ns=np.uint32(lead_time_ns),
        sequenceDuration_ms=np.uint32(sequence_duration_ms),
    )

    return True, args, ""


def _parse_and_validate_lead_time_ns(
    data: Optional[Dict],
) -> Tuple[bool, Optional[int], str]:
    """Parse and validate lead_time_ns from web payload. Returns (ok, value, error_msg)."""
    if data is None:
        data = {}
    raw = data.get("lead_time_ns")
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return False, None, "lead_time_ns: missing"
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        return False, None, "lead_time_ns: not a valid number"
    if val < LEAD_TIME_NS_MIN or val > LEAD_TIME_NS_MAX:
        return False, None, f"lead_time_ns: out of range [{LEAD_TIME_NS_MIN}, {LEAD_TIME_NS_MAX}]"
    if val < 0 or val > UINT32_MAX:
        return False, None, "lead_time_ns: must be in uint32 range"
    return True, val, ""


# Map hysteresis uint16 values to CompHysteresis enum members
_OCD_HYSTERESIS_BY_VALUE = {
    int(m.value): m
    for m in MujoeSerialCommRpi.SetOCDParamsCommandArgs.CompHysteresis
}


def _parse_and_validate_set_ocd_params(
    data: Optional[Dict],
) -> Tuple[bool, Optional[MujoeSerialCommRpi.SetOCDParamsCommandArgs], str]:
    """Parse and validate set-OCD params from web payload. Returns (ok, args, error_msg)."""
    if data is None:
        data = {}
    raw_current = data.get("ocd_max_current_a")
    if raw_current is None or (isinstance(raw_current, str) and raw_current.strip() == ""):
        return False, None, "ocd_max_current_a: missing"
    try:
        ocd_max_current_a = int(float(raw_current))
    except (TypeError, ValueError):
        return False, None, "ocd_max_current_a: not a valid number"
    if ocd_max_current_a < OCD_MAX_CURRENT_A_MIN or ocd_max_current_a > OCD_MAX_CURRENT_A_MAX:
        return False, None, f"ocd_max_current_a: out of range [{OCD_MAX_CURRENT_A_MIN}, {OCD_MAX_CURRENT_A_MAX}]"
    if ocd_max_current_a < 0 or ocd_max_current_a > UINT32_MAX:
        return False, None, "ocd_max_current_a: must be in uint32 range"

    raw_hyst = data.get("input_hysteresis")
    if raw_hyst is None or (isinstance(raw_hyst, str) and raw_hyst.strip() == ""):
        return False, None, "input_hysteresis: missing"
    try:
        hyst_val = int(float(raw_hyst))
    except (TypeError, ValueError):
        return False, None, "input_hysteresis: not a valid number"
    if hyst_val < OCD_INPUT_HYSTERESIS_MIN or hyst_val > OCD_INPUT_HYSTERESIS_MAX:
        return False, None, f"input_hysteresis: out of range [{OCD_INPUT_HYSTERESIS_MIN}, {OCD_INPUT_HYSTERESIS_MAX}]"
    hyst_enum = _OCD_HYSTERESIS_BY_VALUE.get(hyst_val)
    if hyst_enum is None:
        return False, None, "input_hysteresis: invalid selection"

    args = MujoeSerialCommRpi.SetOCDParamsCommandArgs(
        ocdMaxCurrent_A=np.uint32(ocd_max_current_a),
        compHysteresis=hyst_enum,
    )
    return True, args, ""


def _write_last_command_response(
    file_path: str,
    command: str,
    response_type: str,
    response_detail: str,
) -> None:
    """Write last command response to JSON file for the web portal."""
    data_dir = os.path.dirname(file_path)
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    timestamp_iso = datetime.datetime.utcnow().isoformat() + "Z"
    with open(file_path, "w") as f:
        json.dump(
            {
                "command": command,
                "response_type": response_type,
                "response_detail": response_detail,
                "timestamp_iso": timestamp_iso,
            },
            f,
        )


def _write_hv_bus_state(file_path: str, hv_bus_on: bool) -> None:
    """Persist last successful HV direction. Both polarities share the ctrl_hv_bus response string."""
    data_dir = os.path.dirname(file_path)
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    with open(file_path, "w") as f:
        json.dump({"hv_bus_on": bool(hv_bus_on)}, f)


def _read_res_per_cap_progress_seq(file_path: str) -> int:
    """Continue the dump sequence across restarts so the portal never sees seq go backwards."""
    try:
        with open(file_path) as f:
            return int(json.load(f).get("seq", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return 0


def _write_res_per_cap_progress(
    file_path: str,
    seq: int,
    state: str,
    received: int = 0,
    total: int = 0,
    detail: str = "",
) -> None:
    """Persist UART dump progress for the portal bar (state: active/done/error)."""
    data_dir = os.path.dirname(file_path)
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    # Write then rename so a 250 ms poll never reads a half-written file
    tmp_path = file_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(
            {
                "seq": int(seq),
                "state": state,
                "received": int(received),
                "total": int(total),
                "detail": detail,
            },
            f,
        )
    os.replace(tmp_path, file_path)


def _write_res_per_cap_csv(file_path: str, buf: np.ndarray) -> Tuple[bool, str]:
    """Write assembled uint32 capture buffer as one sample per CSV line."""
    try:
        path = os.path.expanduser(str(file_path).strip())
        if not path:
            return False, "empty save path"
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent)
        # One uint32 word per line for easy import into spreadsheets/scripts
        with open(path, "w") as f:
            for word in np.asarray(buf).astype(np.uint32).reshape(-1):
                f.write(f"{int(word)}\n")
        return True, path
    except Exception as e:
        return False, str(e)


def main():
    ###############################################################
    # User Config
    ###############################################################

    process_spawn_method = "fork"  # "fork" for Linux, "spawn" for Windows

    # Serial port config
    serial_port_name = "DRSSTC_RPI4"
    baudrate = 115200
    timeout = 5.0
    # If true, perform hardware reset (via GPIO assertion) on init
    hardware_reset_on_init = True
    # If true, perform software reset (via system reset command) on init
    reset_on_init = False

    # Web portal config
    web_portal_host = "0.0.0.0"
    web_portal_port = 5000

    # OSAL timer process tick period
    osal_timer_tick_period_sec = 1e-6  # 1 us

    # Main loop tick period
    main_loop_tick_period_sec = 1e-3  # 1 ms

    # Default start-burst params for web start_burst action
    default_start_freq_hz = 264000
    default_prf_freq_hz = 200
    default_prf_period_us = (1 / default_prf_freq_hz) * 1e6
    default_prf_ontime_us = 100
    default_lead_time_ns = 0
    default_num_burst_cycles = 1
    # Match firmware mujoe_resdrv start-freq tracking window defaults
    default_start_freq_max_neg_dev_percnt = 25
    default_start_freq_max_pos_dev_percnt = 5
    default_sequence_duration_ms = 1000

    # Optional log file (hazmonitor-style)
    enable_log_file = False
    log_dir = "logs"
    verboseLogInfo = True
    verboseLogError = True

    ###############################################################
    # User Config END
    ###############################################################

    tl = TerminalLogger(enableTimestamp=True)
    if enable_log_file:
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        dt_str = datetime.datetime.fromtimestamp(time.time()).strftime("%Y%m%d_%H%M")
        log_fp = os.path.join(log_dir, f"drsstc_mb_controller_web_log_{dt_str}.txt")
        tl.enableLogFile(fileName=log_fp, logLevel="all", logBatchSize=50)

    ###############################################################
    # OSAL timer process
    ###############################################################

    osal_timer_process = OsalTimerProcess(
        logger=tl,
        verbose=False,
        name="osalTimer",
        tickPeriod_sec=osal_timer_tick_period_sec,
    )

    ###############################################################
    # MujoeSerialCommOsal config and init
    ###############################################################

    port = RPI_SERIAL_PORTS[serial_port_name]
    if len(sys.argv) >= 2:
        port = sys.argv[1]

    config = MujoeSerialCommOsalConfig(
        port=port,
        baudrate=baudrate,
        timeout=timeout,
        hardware_reset_on_init=hardware_reset_on_init,
        reset_on_init=reset_on_init,
    )
    mujoe_osal = MujoeSerialCommOsal(
        osalTimerProcess=osal_timer_process,
        config=config,
        logger=tl,
    )

    ###############################################################
    # Web portal OSAL config
    ###############################################################

    web_portal_config = DrsstcWebPortalAppOsalConfig(
        host=web_portal_host,
        port=web_portal_port,
    )
    web_portal_osal = DrsstcWebPortalAppOsal(
        osalTimerProcess=osal_timer_process,
        config=web_portal_config,
        logger=tl,
    )

    ###############################################################
    # Start OSAL processes
    ###############################################################

    ctx = mp.get_context(process_spawn_method)
    osal_timer_process.start(context=ctx)
    mujoe_osal.startProcess(context=ctx)
    web_portal_osal.startProcess(context=ctx)
    time.sleep(1)
    web_portal_osal.setEventStartWebServer()

    ###############################################################
    # Initialize Mujoe serial and wait for init complete
    ###############################################################

    mujoe_osal.initMujoeSerial()
    init_ready = False
    for _ in range(100):
        msgs = consume_emit_queue(mujoe_osal.getEmitQueue())
        for msg in msgs:
            if msg.Id == MujoeSerialCommEmitMessageIDs.INIT_COMPLETE.value:
                tl.logInfo(f"main:: Init complete, status: '{msg.Data}'")
                init_ready = True
                break
        if init_ready:
            break
        time.sleep(0.1)

    ###############################################################
    # Main loop: drain web portal and mujoe emit queues
    ###############################################################

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    last_start_burst_params_path = os.path.join(
        repo_root, "web_portal_interface", "data", "last_start_burst_params.json"
    )
    last_ocd_params_path = os.path.join(
        repo_root, "web_portal_interface", "data", "last_ocd_params.json"
    )
    last_timed_burst_sequence_params_path = os.path.join(
        repo_root,
        "web_portal_interface",
        "data",
        "last_timed_burst_sequence_params.json",
    )
    last_command_response_path = os.path.join(
        repo_root, "web_portal_interface", "data", "last_command_response.json"
    )
    last_hv_bus_state_path = os.path.join(
        repo_root, "web_portal_interface", "data", "last_hv_bus_state.json"
    )
    default_res_per_cap_csv_path = os.path.join(
        repo_root, "web_portal_interface", "data", "res_per_cap_buffer.csv"
    )
    res_per_cap_progress_path = os.path.join(
        repo_root, "web_portal_interface", "data", "res_per_cap_progress.json"
    )
    res_per_cap_progress_seq = _read_res_per_cap_progress_seq(res_per_cap_progress_path)
    # Last known total so done/error rows still show the transfer size
    res_per_cap_progress_last = {"received": 0, "total": 0}
    pending_start_burst_args: List[MujoeSerialCommRpi.StartBurstCommandArgs] = []
    pending_start_timed_burst_sequence_args: List[
        MujoeSerialCommRpi.StartTimedBurstSequenceCommandArgs
    ] = []
    pending_set_ocd_params_args: List[MujoeSerialCommRpi.SetOCDParamsCommandArgs] = []
    pending_res_per_cap_save_paths: List[str] = []
    # Requested HV polarity for the command in flight; response text does not say on vs off
    pending_hv_bus_on: Optional[bool] = None
    last_sent_command: Optional[str] = None

    elapsed_time = 0.0
    msgObserver = MessageObserver()
    try:
        tl.logInfo(f"main:: Web portal at http://{web_portal_host}:{web_portal_port}/ ; Ctrl+C to exit")
        while True:
            # Drain web portal emit queue and forward to MujoeSerialCommOsal
            web_msgs = consume_emit_queue(web_portal_osal.getEmitQueue())
            for msg in web_msgs:
                if msg.Id == WebPortalEmitMessageIDs.START_BURST_PRESSED.value:
                    ok, args, err = _parse_and_validate_start_burst_params(
                        msg.Data,
                        default_start_freq_hz,
                        default_prf_period_us,
                        default_prf_ontime_us,
                        default_lead_time_ns,
                        default_num_burst_cycles,
                        default_start_freq_max_neg_dev_percnt,
                        default_start_freq_max_pos_dev_percnt,
                    )
                    if not ok:
                        if verboseLogError:
                            tl.logError(f"main:: Start burst validation failed: {err}")
                    else:
                        last_sent_command = "start_burst"
                        mujoe_osal.sendStartBurst(args)
                        pending_start_burst_args.append(args)
                        if verboseLogInfo:
                            tl.logInfo("main:: Start burst command sent (from web)")
                        msgObserver.awaitSerialCommCmdRsp("start_burst")
                elif msg.Id == WebPortalEmitMessageIDs.START_TIMED_BURST_SEQUENCE_PRESSED.value:
                    ok, args, err = _parse_and_validate_timed_burst_sequence_params(
                        msg.Data,
                        default_start_freq_hz,
                        default_prf_period_us,
                        default_prf_ontime_us,
                        default_lead_time_ns,
                        default_sequence_duration_ms,
                    )
                    if not ok:
                        if verboseLogError:
                            tl.logError(
                                f"main:: Start timed burst sequence validation failed: {err}"
                            )
                    else:
                        last_sent_command = "start_timed_burst_sequence"
                        mujoe_osal.sendStartTimedBurstSequence(args)
                        pending_start_timed_burst_sequence_args.append(args)
                        if verboseLogInfo:
                            tl.logInfo(
                                "main:: Start timed burst sequence command sent (from web)"
                            )
                        msgObserver.awaitSerialCommCmdRsp("start_timed_burst_sequence")
                elif msg.Id == WebPortalEmitMessageIDs.STOP_BURST_PRESSED.value:
                    last_sent_command = "stop_burst"
                    mujoe_osal.sendStopBurst()
                    if verboseLogInfo:
                        tl.logInfo("main:: Stop burst command sent (from web)")
                elif msg.Id == WebPortalEmitMessageIDs.POWER_ON_HV_BUS_PRESSED.value:
                    last_sent_command = "power_on_hv_bus"
                    pending_hv_bus_on = True
                    mujoe_osal.sendCtrlHvBus(powerOn=True)
                    if verboseLogInfo:
                        tl.logInfo("main:: Power ON HV bus command sent (from web)")
                elif msg.Id == WebPortalEmitMessageIDs.POWER_OFF_HV_BUS_PRESSED.value:
                    last_sent_command = "power_off_hv_bus"
                    pending_hv_bus_on = False
                    mujoe_osal.sendCtrlHvBus(powerOn=False)
                    if verboseLogInfo:
                        tl.logInfo("main:: Power OFF HV bus command sent (from web)")
                elif msg.Id == WebPortalEmitMessageIDs.SET_LEAD_TIME_PRESSED.value:
                    ok, lead_time_ns, err = _parse_and_validate_lead_time_ns(msg.Data)
                    if not ok:
                        if verboseLogError:
                            tl.logError(f"main:: Set lead time validation failed: {err}")
                        _write_last_command_response(
                            last_command_response_path,
                            "set_lead_time",
                            "error",
                            err,
                        )
                    else:
                        last_sent_command = "set_lead_time"
                        mujoe_osal.sendSetLeadTime(np.uint32(lead_time_ns))
                        msgObserver.awaitSerialCommCmdRsp("set_lead_time")
                        if verboseLogInfo:
                            tl.logInfo("main:: Set lead time command sent (from web)")
                elif msg.Id == WebPortalEmitMessageIDs.SET_OCD_PARAMS_PRESSED.value:
                    ok, ocd_args, err = _parse_and_validate_set_ocd_params(msg.Data)
                    if not ok:
                        if verboseLogError:
                            tl.logError(f"main:: Set OCD params validation failed: {err}")
                        _write_last_command_response(
                            last_command_response_path,
                            "set_ocd_params",
                            "error",
                            err,
                        )
                    else:
                        last_sent_command = "set_ocd_params"
                        mujoe_osal.sendSetOcdParams(ocd_args)
                        pending_set_ocd_params_args.append(ocd_args)
                        msgObserver.awaitSerialCommCmdRsp("set_ocd_params")
                        if verboseLogInfo:
                            tl.logInfo("main:: Set OCD params command sent (from web)")
                elif msg.Id == WebPortalEmitMessageIDs.GET_RES_PER_CAP_BUFFER_PRESSED.value:
                    data = msg.Data if isinstance(msg.Data, dict) else {}
                    save_path = data.get("save_path") or default_res_per_cap_csv_path
                    last_sent_command = "get_res_per_cap_buffer"
                    pending_res_per_cap_save_paths.append(str(save_path))
                    # New seq tells the portal this bar belongs to the dump just requested
                    res_per_cap_progress_seq += 1
                    res_per_cap_progress_last = {"received": 0, "total": 0}
                    _write_res_per_cap_progress(
                        res_per_cap_progress_path, res_per_cap_progress_seq, "active"
                    )
                    mujoe_osal.sendGetResPerCapBuffer()
                    msgObserver.awaitSerialCommCmdRsp("get_res_per_cap_buffer")
                    if verboseLogInfo:
                        tl.logInfo(
                            f"main:: Get res per cap buffer command sent (from web), save_path={save_path}"
                        )
                elif msg.Id == WebPortalEmitMessageIDs.INVALID_ACTION.value:
                    if verboseLogError:
                        tl.logError(f"main:: Invalid action from web: {msg.Data}")
                else:
                    if verboseLogError:
                        tl.logError(f"main:: Unknown action from web: {msg.Id}")

            # Drain MujoeSerialCommOsal emit queue
            msgs = consume_emit_queue(mujoe_osal.getEmitQueue())
            for msg in msgs:
                if msg.Id == MujoeSerialCommEmitMessageIDs.COMMAND_SUCCESS.value:
                    tl.logInfo(f"main:: Command success: '{msg.Data}'")
                    msgObserver.onSerialCommCmdRsp(msg.Data)
                    # Write HV state before the response timestamp so a poll cannot settle on the old flag
                    if msg.Data == "ctrl_hv_bus":
                        hv_on = pending_hv_bus_on
                        if hv_on is None:
                            if last_sent_command == "power_on_hv_bus":
                                hv_on = True
                            elif last_sent_command == "power_off_hv_bus":
                                hv_on = False
                        if hv_on is not None:
                            _write_hv_bus_state(last_hv_bus_state_path, hv_on)
                        pending_hv_bus_on = None
                    _write_last_command_response(
                        last_command_response_path,
                        msg.Data,
                        "success",
                        "success",
                    )
                    if msg.Data == "start_burst" and pending_start_burst_args:
                        args_succeeded = pending_start_burst_args.pop(0)
                        data_dir = os.path.dirname(last_start_burst_params_path)
                        if not os.path.exists(data_dir):
                            os.makedirs(data_dir)
                        with open(last_start_burst_params_path, "w") as f:
                            json.dump(
                                {
                                    "start_freq_hz": int(args_succeeded.StartFrequency_Hz),
                                    "prf_period_us": int(args_succeeded.PRF_period_us),
                                    "prf_ontime_us": int(args_succeeded.PRF_ontime_us),
                                    "lead_time_ns": int(args_succeeded.LeadTime_ns),
                                    "num_burst_cycles": int(args_succeeded.NumBurstCycles),
                                    "start_freq_max_neg_dev_percnt": int(
                                        args_succeeded.StartFreqMaxNegDev_percnt
                                    ),
                                    "start_freq_max_pos_dev_percnt": int(
                                        args_succeeded.StartFreqMaxPosDev_percnt
                                    ),
                                },
                                f,
                            )
                    elif (
                        msg.Data == "start_timed_burst_sequence"
                        and pending_start_timed_burst_sequence_args
                    ):
                        args_succeeded = pending_start_timed_burst_sequence_args.pop(0)
                        data_dir = os.path.dirname(last_timed_burst_sequence_params_path)
                        if not os.path.exists(data_dir):
                            os.makedirs(data_dir)
                        with open(last_timed_burst_sequence_params_path, "w") as f:
                            json.dump(
                                {
                                    "start_freq_hz": int(args_succeeded.StartFrequency_Hz),
                                    "prf_period_us": int(args_succeeded.PRF_period_us),
                                    "prf_ontime_us": int(args_succeeded.PRF_ontime_us),
                                    "lead_time_ns": int(args_succeeded.LeadTime_ns),
                                    "sequence_duration_ms": int(
                                        args_succeeded.SequenceDuration_ms
                                    ),
                                },
                                f,
                            )
                    elif msg.Data == "set_ocd_params" and pending_set_ocd_params_args:
                        ocd_args_succeeded = pending_set_ocd_params_args.pop(0)
                        data_dir = os.path.dirname(last_ocd_params_path)
                        if not os.path.exists(data_dir):
                            os.makedirs(data_dir)
                        with open(last_ocd_params_path, "w") as f:
                            json.dump(
                                {
                                    "ocd_max_current_a": int(ocd_args_succeeded.OCDMaxCurrent_A),
                                    "input_hysteresis": int(
                                        ocd_args_succeeded.InputHysteresis.value
                                    ),
                                },
                                f,
                            )
                elif msg.Id == MujoeSerialCommEmitMessageIDs.RES_PER_CAP_PROGRESS.value:
                    data = msg.Data if isinstance(msg.Data, dict) else {}
                    res_per_cap_progress_last = {
                        "received": int(data.get("received", 0)),
                        "total": int(data.get("total", 0)),
                    }
                    _write_res_per_cap_progress(
                        res_per_cap_progress_path,
                        res_per_cap_progress_seq,
                        "active",
                        res_per_cap_progress_last["received"],
                        res_per_cap_progress_last["total"],
                    )
                elif msg.Id == MujoeSerialCommEmitMessageIDs.RES_PER_CAP_BUFFER.value:
                    # Assembled uint32 array from solicited SOL chunks
                    buf = msg.Data
                    save_path = (
                        pending_res_per_cap_save_paths.pop(0)
                        if pending_res_per_cap_save_paths
                        else default_res_per_cap_csv_path
                    )
                    ok, detail = _write_res_per_cap_csv(save_path, buf)
                    # Always mirror to default path so the portal download link works
                    if ok and os.path.abspath(os.path.expanduser(save_path)) != os.path.abspath(
                        default_res_per_cap_csv_path
                    ):
                        _write_res_per_cap_csv(default_res_per_cap_csv_path, buf)
                    msgObserver.onSerialCommCmdRsp("get_res_per_cap_buffer")
                    # Bar settles before the success badge so both agree on the next poll
                    buf_bytes = int(np.asarray(buf).nbytes)
                    _write_res_per_cap_progress(
                        res_per_cap_progress_path,
                        res_per_cap_progress_seq,
                        "done" if ok else "error",
                        buf_bytes,
                        res_per_cap_progress_last["total"] or buf_bytes,
                        "" if ok else f"CSV write failed: {detail}",
                    )
                    if ok:
                        tl.logInfo(
                            f"main:: Res per cap buffer saved ({len(np.asarray(buf))} words) -> {detail}"
                        )
                        # Empty detail: the portal badge only says Complete. Path stays in the log.
                        _write_last_command_response(
                            last_command_response_path,
                            "get_res_per_cap_buffer",
                            "success",
                            "",
                        )
                    else:
                        if verboseLogError:
                            tl.logError(f"main:: Failed to save res per cap CSV: {detail}")
                        _write_last_command_response(
                            last_command_response_path,
                            "get_res_per_cap_buffer",
                            "error",
                            f"CSV write failed: {detail}",
                        )
                elif msg.Id == MujoeSerialCommEmitMessageIDs.COMMAND_FAILED.value:
                    tl.logError(f"main:: Command failed: '{msg.Data}'")
                    msgObserver.onSerialCommCmdRsp(msg.Data)
                    if pending_res_per_cap_save_paths and msg.Data == "get_res_per_cap_buffer":
                        pending_res_per_cap_save_paths.pop(0)
                    if msg.Data == "get_res_per_cap_buffer":
                        _write_res_per_cap_progress(
                            res_per_cap_progress_path,
                            res_per_cap_progress_seq,
                            "error",
                            res_per_cap_progress_last["received"],
                            res_per_cap_progress_last["total"],
                            "transfer failed or timed out",
                        )
                    # Leave last_hv_bus_state.json alone so the switch returns to the previous success
                    if msg.Data == "ctrl_hv_bus":
                        pending_hv_bus_on = None
                    _write_last_command_response(
                        last_command_response_path,
                        msg.Data if isinstance(msg.Data, str) else (last_sent_command or "unknown"),
                        "error",
                        "command failed",
                    )
                elif msg.Id == MujoeSerialCommEmitMessageIDs.FAULT_OCCURRED.value:
                    flags = msg.Data
                    fault_detail = (
                        f"OCD: {getattr(flags, 'OCD_FAULT', False)}, "
                        f"UVLO: {getattr(flags, 'UVLO_FAULT', False)}, "
                        f"FeedbackTimeout: {getattr(flags, 'FEEDBACK_TIMEOUT_FAULT', False)}"
                    )
                    tl.logError(
                        f"main:: Fault occurred - OCD: {getattr(flags, 'OCD_FAULT', '?')} "
                        f"UVLO: {getattr(flags, 'UVLO_FAULT', '?')} "
                        f"FeedbackTimeout: {getattr(flags, 'FEEDBACK_TIMEOUT_FAULT', '?')}"
                    )
                    _write_last_command_response(
                        last_command_response_path,
                        last_sent_command or "unknown",
                        "fault",
                        fault_detail,
                    )

            time.sleep(main_loop_tick_period_sec)
            elapsed_time += main_loop_tick_period_sec
    except KeyboardInterrupt:
        tl.logInfo("main:: User terminated main loop, cleaning up...")

    ###############################################################
    # Cleanup
    ###############################################################

    web_portal_osal.terminateProcess()
    mujoe_osal.terminateProcess()
    osal_timer_process.terminate()
    tl.logInfo("main:: Cleanup complete, exiting...")
    if enable_log_file:
        tl.deInit()


if __name__ == "__main__":
    main()
