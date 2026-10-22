"""
OSAL task wrapper for the DRSSTC web portal Flask app.
Wraps the web server in an OSAL process. Button press events (start burst, stop burst)
are emitted as OsalEmitMessage on the task's emit queue.
"""
import sys
import os
import threading

# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import List
import queue as queue_module
import multiprocessing as mp
import time

from enum import Enum

from osal.osal_process import OsalProcess, OsalProcessConfig, OsalTimerProcess, OsalHandle
from logging_interface.logger import Logger, TerminalLogger
from osal.osal_types import OsalEmitMessage

################################################################
# Emit message IDs (task -> main)
################################################################


class WebPortalEmitMessageIDs(Enum):
    START_BURST_PRESSED = 1
    STOP_BURST_PRESSED = 2
    POWER_ON_HV_BUS_PRESSED = 3
    POWER_OFF_HV_BUS_PRESSED = 4
    INVALID_ACTION = 5
    SET_LEAD_TIME_PRESSED = 6
    SET_OCD_PARAMS_PRESSED = 7
    START_TIMED_BURST_SEQUENCE_PRESSED = 8
    GET_RES_PER_CAP_BUFFER_PRESSED = 9


################################################################
# Event IDs and handler args
################################################################


class EventIds(Enum):
    StartWebServer = 1


class EventHandlerArgs(object):
    Host: str
    Port: int

    def __init__(self, host: str = "0.0.0.0", port: int = 5000):
        self.Host = host
        self.Port = port


################################################################
# Event handlers (module scope for pickling)
################################################################


def startWebServerHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    # import the app module here to avoid import errors 
    # when the app is imported in the main thread
    try:
        from web_portal_interface.drsstc_web_portal_app import app
    except Exception as e:
        osalHdl.logError(f"Failed to import Flask app: {e}")
        return

    # Inject emit callback and IDs so perform_action() can emit button events
    app.config["web_portal_emit"] = lambda emit_id, data: osalHdl.emitMessage(
        OsalEmitMessage(emit_id, data)
    )
    app.config["web_portal_emit_ids"] = {
        "start_burst": WebPortalEmitMessageIDs.START_BURST_PRESSED.value,
        "stop_burst": WebPortalEmitMessageIDs.STOP_BURST_PRESSED.value,
        "power_on_hv_bus": WebPortalEmitMessageIDs.POWER_ON_HV_BUS_PRESSED.value,
        "power_off_hv_bus": WebPortalEmitMessageIDs.POWER_OFF_HV_BUS_PRESSED.value,
        "invalid": WebPortalEmitMessageIDs.INVALID_ACTION.value,
        "set_lead_time": WebPortalEmitMessageIDs.SET_LEAD_TIME_PRESSED.value,
        "set_ocd_params": WebPortalEmitMessageIDs.SET_OCD_PARAMS_PRESSED.value,
        "start_timed_burst_sequence": WebPortalEmitMessageIDs.START_TIMED_BURST_SEQUENCE_PRESSED.value,
        "get_res_per_cap_buffer": WebPortalEmitMessageIDs.GET_RES_PER_CAP_BUFFER_PRESSED.value,
    }

    osalHdl.logInfo(f"Starting web server on {args.Host}:{args.Port}")

    def run_flask():
        try:
            app.run(
                host=args.Host,
                port=args.Port,
                debug=False,
                threaded=True,
                use_reloader=False,
            )
        except Exception as e:
            osalHdl.logError(f"Flask run error: {e}")

    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    osalHdl.logInfo("Web server thread started")


################################################################
# Config and wrapper class
################################################################


class DrsstcWebPortalAppOsalConfig(object):
    _osalConfig: OsalProcessConfig
    Host: str
    Port: int

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 5000,
        rx_queue_size: int = 20,
        emit_queue_size: int = 20,
    ):
        self.Host = host
        self.Port = port
        self._osalConfig = OsalProcessConfig.create_from_preset(
            OsalProcessConfig.Presets.DEFAULT
        )
        self._osalConfig.Name = "drsstcWebPortal"
        self._osalConfig.RxQueueSize = rx_queue_size
        self._osalConfig.EmitQueueSize = emit_queue_size
        self._osalConfig.UserMessageHandler = None


class DrsstcWebPortalAppOsal(object):
    Config: DrsstcWebPortalAppOsalConfig
    _osal: OsalProcess
    _osalTimerProcess: OsalTimerProcess
    _logger: Logger

    def __init__(
        self,
        osalTimerProcess: OsalTimerProcess,
        config: DrsstcWebPortalAppOsalConfig,
        logger: Logger = None,
    ):
        self.Config = config
        self._osalTimerProcess = osalTimerProcess
        self._logger = logger

        self._osal = OsalProcess(
            osalTimerProcess=self._osalTimerProcess,
            config=self.Config._osalConfig,
            logger=self._logger,
        )

        args = EventHandlerArgs(host=self.Config.Host, port=self.Config.Port)
        self._osal.setEventHandlerArgs(args)
        self._osal.registerEvent(EventIds.StartWebServer.value, startWebServerHandler)

    def startProcess(self, context: mp.context.SpawnContext):
        """Start the underlying OSAL process."""
        self._osal.start(context=context)

    def getEmitQueue(self) -> mp.Queue:
        """Get the emit queue for consuming button press messages."""
        return self._osal.EmitQueue

    def terminateProcess(self):
        """Stop the underlying OSAL process."""
        self._osal.terminate()

    def setEventStartWebServer(self):
        """Trigger the StartWebServer event to start the Flask server in-process."""
        self._osal.setEvent(EventIds.StartWebServer.value)


################################################################
# Helpers
################################################################


def consume_emit_queue(q: mp.Queue) -> List[OsalEmitMessage]:
    msgs: List[OsalEmitMessage] = []
    while True:
        try:
            msg = q.get(block=False)
            if msg is not None:
                msgs.append(msg)
        except queue_module.Empty:
            break
    return msgs


################################################################
# Main: run web portal OSAL and drain emit queue
################################################################


def main():
    # process spawn method, use "fork" for linux and "spawn" for windows
    process_spawn_method = "fork"
    host = "0.0.0.0"
    port = 5000
    osal_timer_tick_period_sec = 1e-6
    main_loop_tick_period_sec = 0.5

    tl = TerminalLogger(enableTimestamp=True)
    osalTimerProcess = OsalTimerProcess(
        logger=tl,
        verbose=False,
        name="osalTimer",
        tickPeriod_sec=osal_timer_tick_period_sec,
    )

    config = DrsstcWebPortalAppOsalConfig(host=host, port=port)
    webPortalOsal = DrsstcWebPortalAppOsal(
        osalTimerProcess=osalTimerProcess, config=config, logger=tl
    )

    ctx = mp.get_context(process_spawn_method)

    osalTimerProcess.start(context=ctx)
    webPortalOsal.startProcess(context=ctx)
    webPortalOsal.setEventStartWebServer()

    tl.logInfo(f"Web portal OSAL running; open http://{host}:{port}/")
    tl.logInfo("Press Ctrl+C to exit")

    try:
        while True:
            msgs = consume_emit_queue(webPortalOsal.getEmitQueue())
            for msg in msgs:
                if msg.Id == WebPortalEmitMessageIDs.START_BURST_PRESSED.value:
                    tl.logInfo(f"Emit: START_BURST_PRESSED data={msg.Data}")
                elif msg.Id == WebPortalEmitMessageIDs.STOP_BURST_PRESSED.value:
                    tl.logInfo(f"Emit: STOP_BURST_PRESSED data={msg.Data}")
                elif msg.Id == WebPortalEmitMessageIDs.SET_LEAD_TIME_PRESSED.value:
                    tl.logInfo(f"Emit: SET_LEAD_TIME_PRESSED data={msg.Data}")
                elif msg.Id == WebPortalEmitMessageIDs.SET_OCD_PARAMS_PRESSED.value:
                    tl.logInfo(f"Emit: SET_OCD_PARAMS_PRESSED data={msg.Data}")
                elif msg.Id == WebPortalEmitMessageIDs.START_TIMED_BURST_SEQUENCE_PRESSED.value:
                    tl.logInfo(f"Emit: START_TIMED_BURST_SEQUENCE_PRESSED data={msg.Data}")
                elif msg.Id == WebPortalEmitMessageIDs.INVALID_ACTION.value:
                    tl.logInfo(f"Emit: INVALID_ACTION data={msg.Data}")
                else:
                    tl.logInfo(f"Emit: unknown id={msg.Id} data={msg.Data}")
            time.sleep(main_loop_tick_period_sec)
    except KeyboardInterrupt:
        tl.logInfo("Keyboard interrupt, terminating...")
    finally:
        webPortalOsal.terminateProcess()
        osalTimerProcess.terminate()
        tl.logInfo("Cleanup complete, exiting...")


if __name__ == "__main__":
    main()
