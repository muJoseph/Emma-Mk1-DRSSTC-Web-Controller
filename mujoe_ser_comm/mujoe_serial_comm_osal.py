"""
OSAL task wrapper for MujoeSerialCommRpi.
Wraps the RPi serial driver in an OSAL process for use in multithreaded/multiprocess
environments. User commands (start burst, stop burst, reset, ack fault) are forwarded
via the thread-safe messaging API (sendUserMessage / OsalMessageUserMessage).
"""
import sys
import os

# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import List
import queue
import multiprocessing as mp
import numpy as np

from enum import Enum
import time

from osal.osal_process import OsalProcess, OsalProcessConfig, OsalTimerProcess, OsalHandle
from logging_interface.logger import Logger, TerminalLogger
from osal.osal_types import OsalMessageUserMessage, OsalEmitMessage

from mujoe_ser_comm.mujoe_serial_comm_rpi import MujoeSerialCommRpi, RPI_SERIAL_PORTS

################################################################
# Emit message IDs (task -> main)
################################################################


class MujoeSerialCommEmitMessageIDs(Enum):
    INIT_COMPLETE = 1
    COMMAND_SUCCESS = 2
    COMMAND_FAILED = 3
    FAULT_OCCURRED = 4
    RES_PER_CAP_BUFFER = 5
    # Data: {"received": bytes, "total": bytes}; total is 0 until the first SOL chunk
    RES_PER_CAP_PROGRESS = 6


################################################################
# Event IDs and handler args
################################################################


class EventIds(Enum):
    InitMujoeSerial = 1
    StartBurst = 2
    StopBurst = 3
    Reset = 4
    AckFault = 5
    FaultOccurred = 6
    CtrlHvBus = 7
    SetLeadTime = 8
    SetOcdParams = 9
    StartTimedBurstSequence = 10
    GetResPerCapBuffer = 11


class EventHandlerArgs(object):
    Verbose: bool
    SerComm: MujoeSerialCommRpi
    Port: str
    Baudrate: int
    Timeout: float
    HardwareResetOnInit: bool
    ResetOnInit: bool
    StartBurstArgs: object  # MujoeSerialCommRpi.StartBurstCommandArgs or None
    StartTimedBurstSequenceArgs: object  # MujoeSerialCommRpi.StartTimedBurstSequenceCommandArgs or None
    HvBusPowerOn: bool
    def __init__(
        self,
        port: str,
        baudrate: int,
        timeout: float,
        hardware_reset_on_init: bool = True,
        reset_on_init: bool = False,
    ):
        self.Verbose = False
        self.Port = port
        self.Baudrate = baudrate
        self.Timeout = timeout
        self.HardwareResetOnInit = hardware_reset_on_init
        self.ResetOnInit = reset_on_init
        self.StartBurstArgs = None
        self.StartTimedBurstSequenceArgs = None
        self.HvBusPowerOn = False

################################################################
# Event handlers (module scope for pickling)
################################################################


def initMujoeSerialHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    args.SerComm = MujoeSerialCommRpi(
        port=args.Port,
        baudrate=args.Baudrate,
        timeout=args.Timeout,
        logger=osalHdl._logger,
    )
    osalHdl.logInfo("MujoeSerialCommRpi initialized")

    if not args.SerComm.open():
        osalHdl.logError("Mujoe serial comm failed to open..aborting")
        return

    if args.Verbose:
        osalHdl.logInfo("Initializing Mujoe serial module...")

    if args.HardwareResetOnInit:
        osalHdl.logInfo("Hardware reset requested...performing")
        if not args.SerComm.hardwareReset():
            osalHdl.logError("Hardware reset failed..retrying")
            osalHdl.setEvent(EventIds.InitMujoeSerial.value, delay_seconds=1)
            return
    else:
        osalHdl.logInfo("Hardware reset NOT requested...skipping")

    if args.ResetOnInit:
        osalHdl.logInfo("Software reset requested...performing")
        if not args.SerComm.perform_system_reset_await_response():
            osalHdl.logError("Software reset failed..retrying")
            osalHdl.setEvent(EventIds.InitMujoeSerial.value, delay_seconds=1)
            return
    else:
        osalHdl.logInfo("Software reset NOT requested...skipping")

    osalHdl.logInfo("Mujoe serial module initialized successfully")
    osalHdl.emitMessage(
        OsalEmitMessage(MujoeSerialCommEmitMessageIDs.INIT_COMPLETE.value, "success")
    )


def startBurstHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    if args.StartBurstArgs is None:
        osalHdl.logError("StartBurstHandler: StartBurstArgs is None")
        return
    ret = args.SerComm.start_burst_await_response(args.StartBurstArgs)
    if ret:
        osalHdl.logInfo("Start burst successful")
        osalHdl.emitMessage(
            OsalEmitMessage(MujoeSerialCommEmitMessageIDs.COMMAND_SUCCESS.value, "start_burst")
        )
    else:
        osalHdl.logError("Start burst failed...checking for faults")
        if args.SerComm.CmdRspFlags is not None and args.SerComm.CmdRspFlags.IsFaultActive:
            osalHdl.emitMessage(
                OsalEmitMessage(
                    MujoeSerialCommEmitMessageIDs.FAULT_OCCURRED.value,
                    args.SerComm.CmdRspFlags,
                )
            )
            osalHdl.setEvent(EventIds.FaultOccurred.value)
            if args.Verbose:
                osalHdl.logInfo(f"OCD fault: {args.SerComm.CmdRspFlags.OCD_FAULT}")
                osalHdl.logInfo(f"UVLO fault: {args.SerComm.CmdRspFlags.UVLO_FAULT}")
                osalHdl.logInfo(
                    f"Feedback timeout fault: {args.SerComm.CmdRspFlags.FEEDBACK_TIMEOUT_FAULT}"
                )
        else:
            osalHdl.setEvent(EventIds.StartBurst.value, delay_seconds=1)


def startTimedBurstSequenceHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    if args.StartTimedBurstSequenceArgs is None:
        osalHdl.logError("StartTimedBurstSequenceHandler: StartTimedBurstSequenceArgs is None")
        return
    ret = args.SerComm.start_timed_burst_sequence_await_response(args.StartTimedBurstSequenceArgs)
    if ret:
        osalHdl.logInfo("Start timed burst sequence successful")
        osalHdl.emitMessage(
            OsalEmitMessage(
                MujoeSerialCommEmitMessageIDs.COMMAND_SUCCESS.value,
                "start_timed_burst_sequence",
            )
        )
    else:
        osalHdl.logError("Start timed burst sequence failed...checking for faults")
        if args.SerComm.CmdRspFlags is not None and args.SerComm.CmdRspFlags.IsFaultActive:
            osalHdl.emitMessage(
                OsalEmitMessage(
                    MujoeSerialCommEmitMessageIDs.FAULT_OCCURRED.value,
                    args.SerComm.CmdRspFlags,
                )
            )
            osalHdl.setEvent(EventIds.FaultOccurred.value)
            if args.Verbose:
                osalHdl.logInfo(f"OCD fault: {args.SerComm.CmdRspFlags.OCD_FAULT}")
                osalHdl.logInfo(f"UVLO fault: {args.SerComm.CmdRspFlags.UVLO_FAULT}")
                osalHdl.logInfo(
                    f"Feedback timeout fault: {args.SerComm.CmdRspFlags.FEEDBACK_TIMEOUT_FAULT}"
                )
        else:
            osalHdl.setEvent(EventIds.StartTimedBurstSequence.value, delay_seconds=1)


def stopBurstHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    ret = args.SerComm.stop_burst_await_response()
    if ret:
        osalHdl.logInfo("Stop burst successful")
        osalHdl.emitMessage(
            OsalEmitMessage(MujoeSerialCommEmitMessageIDs.COMMAND_SUCCESS.value, "stop_burst")
        )
    else:
        osalHdl.logError("Stop burst failed")

def setLeadTimeHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    ret = args.SerComm.set_lead_time_await_response(leadTime_ns=args.LeadTime_ns)
    if ret:
        osalHdl.logInfo("Set lead time successful")
        osalHdl.emitMessage(
            OsalEmitMessage(MujoeSerialCommEmitMessageIDs.COMMAND_SUCCESS.value, "set_lead_time")
        )
    else:
        osalHdl.logError("Set lead time failed")


def setOcdParamsHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    ret = args.SerComm.set_ocd_params_await_response(setOCDParamsCommandArgs=args.SetOCDParamsArgs)
    if ret:
        osalHdl.logInfo("Set OCD params successful")
        osalHdl.emitMessage(
            OsalEmitMessage(MujoeSerialCommEmitMessageIDs.COMMAND_SUCCESS.value, "set_ocd_params")
        )
    else:
        osalHdl.logError("Set OCD params failed")


def ctrlHvBusHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    # Send command
    ret = args.SerComm.ctrl_hv_bus_await_response(powerOn=args.HvBusPowerOn)
    cmdRetStatus = MujoeSerialCommEmitMessageIDs.COMMAND_FAILED.value

    # Process cmd response
    if ret:
        if args.Verbose:
            osalHdl.logInfo(f"Control HV bus command successful")

        cmdRetStatus = MujoeSerialCommEmitMessageIDs.COMMAND_SUCCESS.value
    else:
        if args.Verbose:
            osalHdl.logError(f"Control HV bus command failed!")

    # Emit message to host
    osalHdl.emitMessage(
            OsalEmitMessage(cmdRetStatus, "ctrl_hv_bus")
        )

def resetHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    ret = args.SerComm.perform_system_reset_await_response()
    if ret:
        osalHdl.logInfo("Reset successful")
        osalHdl.emitMessage(
            OsalEmitMessage(MujoeSerialCommEmitMessageIDs.COMMAND_SUCCESS.value, "reset")
        )
    else:
        osalHdl.logError("Reset failed")


def ackFaultHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    ret = args.SerComm.ack_fault_await_response()
    if ret:
        osalHdl.logInfo("Fault acknowledged successfully")
        osalHdl.emitMessage(
            OsalEmitMessage(MujoeSerialCommEmitMessageIDs.COMMAND_SUCCESS.value, "ack_fault")
        )
    else:
        osalHdl.logError("Fault acknowledgment failed, retrying...")
        osalHdl.setEvent(EventIds.AckFault.value, delay_seconds=1)


def faultOccurredHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    ret = args.SerComm.ack_fault_await_response()
    if ret:
        osalHdl.logInfo("Fault acknowledged successfully")
    else:
        osalHdl.logError("Fault acknowledgment failed, retrying...")
        osalHdl.setEvent(EventIds.FaultOccurred.value, delay_seconds=1)


def getResPerCapBufferHandler(osalHdl: OsalHandle, args: EventHandlerArgs):
    # Blocks until SOL chunks assemble; progress is emitted from inside that loop so the portal can draw a bar
    def onProgress(received: int, total: int):
        osalHdl.emitMessage(
            OsalEmitMessage(
                MujoeSerialCommEmitMessageIDs.RES_PER_CAP_PROGRESS.value,
                {"received": int(received), "total": int(total)},
            )
        )

    buf = args.SerComm.get_res_per_cap_buffer_await_response(progress_cb=onProgress)
    if buf is not None:
        osalHdl.logInfo(f"Res period capture buffer received ({len(buf)} words)")
        osalHdl.emitMessage(
            OsalEmitMessage(MujoeSerialCommEmitMessageIDs.RES_PER_CAP_BUFFER.value, buf)
        )
    else:
        osalHdl.logError("Get res period capture buffer failed")
        osalHdl.emitMessage(
            OsalEmitMessage(
                MujoeSerialCommEmitMessageIDs.COMMAND_FAILED.value,
                "get_res_per_cap_buffer",
            )
        )


################################################################
# User message handler
################################################################


def handleUserMessageEvent(
    osalHdl: OsalHandle, msg: OsalMessageUserMessage, args: EventHandlerArgs
):
    if msg.UserMessageId == MujoeSerialCommOsal.UserMessageIds.START_BURST.value:
        args.StartBurstArgs = msg.Data
        osalHdl.setEvent(EventIds.StartBurst.value)
    elif msg.UserMessageId == MujoeSerialCommOsal.UserMessageIds.STOP_BURST.value:
        osalHdl.setEvent(EventIds.StopBurst.value)
    elif msg.UserMessageId == MujoeSerialCommOsal.UserMessageIds.RESET.value:
        osalHdl.setEvent(EventIds.Reset.value)
    elif msg.UserMessageId == MujoeSerialCommOsal.UserMessageIds.ACK_FAULT.value:
        osalHdl.setEvent(EventIds.AckFault.value)
    elif msg.UserMessageId == MujoeSerialCommOsal.UserMessageIds.CTRL_HV_BUS.value:
        args.HvBusPowerOn = msg.Data
        osalHdl.setEvent(EventIds.CtrlHvBus.value)
    elif msg.UserMessageId == MujoeSerialCommOsal.UserMessageIds.SET_LEAD_TIME.value:
        args.LeadTime_ns = msg.Data
        osalHdl.setEvent(EventIds.SetLeadTime.value)
    elif msg.UserMessageId == MujoeSerialCommOsal.UserMessageIds.SET_OCD_PARAMS.value:
        args.SetOCDParamsArgs = msg.Data
        osalHdl.setEvent(EventIds.SetOcdParams.value)
    elif msg.UserMessageId == MujoeSerialCommOsal.UserMessageIds.START_TIMED_BURST_SEQUENCE.value:
        args.StartTimedBurstSequenceArgs = msg.Data
        osalHdl.setEvent(EventIds.StartTimedBurstSequence.value)
    elif msg.UserMessageId == MujoeSerialCommOsal.UserMessageIds.GET_RES_PER_CAP_BUFFER.value:
        osalHdl.setEvent(EventIds.GetResPerCapBuffer.value)


################################################################
# Config and wrapper class
################################################################


class MujoeSerialCommOsalConfig(object):
    _osalConfig: OsalProcessConfig
    Port: str
    Baudrate: int
    Timeout: float
    HardwareResetOnInit: bool
    ResetOnInit: bool

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 5.0,
        hardware_reset_on_init: bool = True,
        reset_on_init: bool = False,
    ):
        q_size = 100
        self.Port = port
        self.Baudrate = baudrate
        self.Timeout = timeout
        self.HardwareResetOnInit = hardware_reset_on_init
        self.ResetOnInit = reset_on_init
        self._osalConfig = OsalProcessConfig.create_from_preset(
            OsalProcessConfig.Presets.DEFAULT
        )
        self._osalConfig.Name = "mujoeSerialComm"
        self._osalConfig.RxQueueSize = q_size
        self._osalConfig.EmitQueueSize = q_size
        self._osalConfig.UserMessageHandler = handleUserMessageEvent


class MujoeSerialCommOsal(object):
    Config: MujoeSerialCommOsalConfig
    _osal: OsalProcess
    _osalTimerProcess: OsalTimerProcess
    _logger: Logger

    class UserMessageIds(Enum):
        START_BURST = 1
        STOP_BURST = 2
        RESET = 3
        ACK_FAULT = 4
        CTRL_HV_BUS = 5
        SET_LEAD_TIME = 6
        SET_OCD_PARAMS = 7
        START_TIMED_BURST_SEQUENCE = 8
        GET_RES_PER_CAP_BUFFER = 9

    def __init__(
        self,
        osalTimerProcess: OsalTimerProcess,
        config: MujoeSerialCommOsalConfig,
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

        args = EventHandlerArgs(
            port=self.Config.Port,
            baudrate=self.Config.Baudrate,
            timeout=self.Config.Timeout,
            hardware_reset_on_init=self.Config.HardwareResetOnInit,
            reset_on_init=self.Config.ResetOnInit,
        )
        args.Verbose = True

        self._osal.setEventHandlerArgs(args)

        self._osal.registerEvent(EventIds.InitMujoeSerial.value, initMujoeSerialHandler)
        self._osal.registerEvent(EventIds.StartBurst.value, startBurstHandler)
        self._osal.registerEvent(EventIds.StopBurst.value, stopBurstHandler)
        self._osal.registerEvent(EventIds.Reset.value, resetHandler)
        self._osal.registerEvent(EventIds.AckFault.value, ackFaultHandler)
        self._osal.registerEvent(EventIds.FaultOccurred.value, faultOccurredHandler)
        self._osal.registerEvent(EventIds.CtrlHvBus.value, ctrlHvBusHandler)
        self._osal.registerEvent(EventIds.SetLeadTime.value, setLeadTimeHandler)
        self._osal.registerEvent(EventIds.SetOcdParams.value, setOcdParamsHandler)
        self._osal.registerEvent(
            EventIds.StartTimedBurstSequence.value, startTimedBurstSequenceHandler
        )
        self._osal.registerEvent(
            EventIds.GetResPerCapBuffer.value, getResPerCapBufferHandler
        )

    def startProcess(self, context: mp.context.SpawnContext):
        """Start the underlying OSAL process."""
        self._osal.start(context=context)

    def getEmitQueue(self) -> mp.Queue:
        """Get the emit queue for consuming status messages."""
        return self._osal.EmitQueue

    def terminateProcess(self):
        """Stop the underlying OSAL process."""
        self._osal.terminate()

    def initMujoeSerial(self):
        """Trigger initialization of the Mujoe serial module."""
        self._osal.setEvent(EventIds.InitMujoeSerial.value)

    def sendStartBurst(self, args: MujoeSerialCommRpi.StartBurstCommandArgs):
        msg = OsalMessageUserMessage(self.UserMessageIds.START_BURST.value, args)
        self._osal.sendMessage(msg)

    def sendStopBurst(self):
        self._osal.sendUserMessage(self.UserMessageIds.STOP_BURST.value, None)

    def sendCtrlHvBus(self, powerOn: bool):
        msg = OsalMessageUserMessage(self.UserMessageIds.CTRL_HV_BUS.value, powerOn)
        self._osal.sendMessage(msg)

    def sendReset(self):
        self._osal.sendUserMessage(self.UserMessageIds.RESET.value, None)

    def sendAckFault(self):
        self._osal.sendUserMessage(self.UserMessageIds.ACK_FAULT.value, None)

    def sendSetLeadTime(self, leadTime_ns: np.uint32):
        msg = OsalMessageUserMessage(self.UserMessageIds.SET_LEAD_TIME.value, leadTime_ns)
        self._osal.sendMessage(msg)

    def sendSetOcdParams(self, args: MujoeSerialCommRpi.SetOCDParamsCommandArgs):
        msg = OsalMessageUserMessage(self.UserMessageIds.SET_OCD_PARAMS.value, args)
        self._osal.sendMessage(msg)

    def sendStartTimedBurstSequence(
        self, args: MujoeSerialCommRpi.StartTimedBurstSequenceCommandArgs
    ):
        msg = OsalMessageUserMessage(
            self.UserMessageIds.START_TIMED_BURST_SEQUENCE.value, args
        )
        self._osal.sendMessage(msg)

    def sendGetResPerCapBuffer(self):
        self._osal.sendUserMessage(self.UserMessageIds.GET_RES_PER_CAP_BUFFER.value, None)


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
        except queue.Empty:
            break
    return msgs


################################################################
# Main: command prompt menu
################################################################


def main():

    ###############################################################
    # User Config
    ###############################################################

    process_spawn_method = "fork" # Process spawn method, use "fork" for linux and "spawn" for windows

    # Serial port config
    serial_port_name = "DRSSTC_RPI4"
    baudrate = 115200
    timeout = 5.0
    # Hardware reset on init
    hardware_reset_on_init = True
    # Software reset on init
    reset_on_init = False

    # OSAL timer process tick period
    osal_timer_tick_period_sec = 1e-6 # 1 us

    # Main loop tick period,
    main_loop_tick_period_sec = 1e-3 # 1 ms

    ###############################################################
    # User Config END
    ###############################################################
    tl = TerminalLogger(enableTimestamp=True)
    osalTimerProcess = OsalTimerProcess(
        logger=tl, verbose=False, name="osalTimer", tickPeriod_sec=osal_timer_tick_period_sec
    )

    # Get port for serial port name LUT
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
    mujoeOsal = MujoeSerialCommOsal(
        osalTimerProcess=osalTimerProcess, config=config, logger=tl
    )

    # Get the spawn context so that it can be used to create child processes
    ctx = mp.get_context(process_spawn_method)

    # Start timer process
    osalTimerProcess.start(context=ctx)

    # Start Mujoe serial comm OSAL process
    mujoeOsal.startProcess(context=ctx)

    # Initialize the Mujoe serial comm
    mujoeOsal.initMujoeSerial()

    # Wait for init complete
    init_ready = False
    for _ in range(100):
        msgs = consume_emit_queue(mujoeOsal.getEmitQueue())
        for msg in msgs:
            if msg.Id == MujoeSerialCommEmitMessageIDs.INIT_COMPLETE.value:
                tl.logInfo(f"main:: Init complete, status: '{msg.Data}'")
                init_ready = True
                break
        if init_ready:
            break
        time.sleep(0.1)

    if not init_ready:
        tl.logError("main:: Init did not complete in time")
        mujoeOsal.terminateProcess()
        osalTimerProcess.terminate()
        return

    # Self-test default burst parameters
    default_start_freq_hz = 264000
    default_prf_freq_hz = 200
    default_prf_period_us = (1 / default_prf_freq_hz) * 1e6
    default_prf_ontime_us = 100
    default_lead_time_ns = 0

    running = True
    elapsed_time = 0.0
    while running:
        # Drain emit queue and print status
        msgs = consume_emit_queue(mujoeOsal.getEmitQueue())
        for msg in msgs:
            if msg.Id == MujoeSerialCommEmitMessageIDs.COMMAND_SUCCESS.value:
                tl.logInfo(f"main:: Command success: '{msg.Data}'")
            elif msg.Id == MujoeSerialCommEmitMessageIDs.FAULT_OCCURRED.value:
                flags = msg.Data
                tl.logError(
                    f"main:: Fault occurred - OCD: {getattr(flags, 'OCD_FAULT', '?')} "
                    f"UVLO: {getattr(flags, 'UVLO_FAULT', '?')} "
                    f"FeedbackTimeout: {getattr(flags, 'FEEDBACK_TIMEOUT_FAULT', '?')}"
                )

        print()
        print("1. Start burst  2. Stop burst  3. Reset  4. Ack fault  5. Quit")
        try:
            choice = input("Choice [1-5]: ").strip() or "5"
        except (EOFError, KeyboardInterrupt):
            choice = "5"

        if choice == "1":
            sb_args = MujoeSerialCommRpi.StartBurstCommandArgs(
                startFrequency_Hz=np.uint32(default_start_freq_hz),
                prfPeriod_us=np.uint32(default_prf_period_us),
                prfOntime_us=np.uint32(default_prf_ontime_us),
                leadTime_ns=np.uint32(default_lead_time_ns),
                startFreqMaxNegDev_percnt=np.uint8(25),
                startFreqMaxPosDev_percnt=np.uint8(5),
            )
            mujoeOsal.sendStartBurst(sb_args)
            tl.logInfo("main:: Start burst command sent")
        elif choice == "2":
            mujoeOsal.sendStopBurst()
            tl.logInfo("main:: Stop burst command sent")
        elif choice == "3":
            mujoeOsal.sendReset()
            tl.logInfo("main:: Reset command sent")
        elif choice == "4":
            mujoeOsal.sendAckFault()
            tl.logInfo("main:: Ack fault command sent")
        elif choice == "5":
            running = False
        else:
            print("Invalid choice, enter 1-5")


        # Sleep for main loop tick period
        time.sleep(main_loop_tick_period_sec)
        elapsed_time += main_loop_tick_period_sec

    mujoeOsal.terminateProcess()
    osalTimerProcess.terminate()
    tl.logInfo("main:: Cleanup complete, exiting...")


if __name__ == "__main__":
    main()
