"""
Raspberry Pi (Linux) serial variant of MujoeSerialComm.
Uses Linux serial device paths (e.g. /dev/ttyAMA0, /dev/ttyS0) and
post-open buffer reset for a clean Linux serial state.
"""
import sys
import os
import time

# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from logging_interface import TerminalLogger, Logger
import serial
from mujoe_ser_comm.crc import calculate_crc16_ccitt
from enum import Enum
from typing import List

import RPi.GPIO as GPIO # for the reset button

# Raspberry Pi / Linux serial port names (mirrors rpi_config/serial_port pattern)
RPI_SERIAL_PORTS = {
    "RPI4": "/dev/ttyS0",        # Raspberry Pi 4
    "RPI5": "/dev/ttyAMA0",      # Raspberry Pi 5
    "LARON": "/dev/serial0",     # Alternate (e.g. Laron's code)
    "DRSSTC_RPI4": "/dev/serial0",
}


class MujoeSerialCommRpi:

    cmd_args_length = 40
    cmd_rsp_params_length = 40
    cmd_rsp_frame_length = 62
    # Matches MUJOE_SC_MAX_SOL_DATA_CHUNK_LEN / sizeof(mujoe_sc_sol_data_t) + frame base
    sol_data_chunk_max_len = 40
    sol_frame_length = 58
    # little endian
    preamble : np.ndarray = np.array([0xEF, 0xBE, 0xAD, 0xDE], dtype=np.uint8)

    CmdRspFlags = None

    # We are using BCM (Broadcom SOC channel) numbering, so 25 refers to GPIO 25.
    # This is the GPIO pin that is connected to the NRST pin of the DRSSTC.
    DRSSTC_NRST_GPIO = 25

    class ParserState(Enum):
        IDLE = 0
        FRAME_ID = 1
        PAYLOAD_LENGTH = 2
        PAYLOAD = 3
        CRC = 4

    class FrameId(Enum):
        COMMAND = np.uint16(0x0001)
        COMMAND_RESPONSE = np.uint16(0x0002)
        UNSOLICITED = np.uint16(0x0003)
        SOLICITED = np.uint16(0x0004)

    class CommandId(Enum):
        WHO_AM_I = np.uint16(0x0001)
        RESET = np.uint16(0x0002)
        START_BURST = np.uint16(0x0003)
        STOP_BURST = np.uint16(0x0004)
        ACK_FAULT = np.uint16(0x0005)
        CTRL_HV_BUS = np.uint16(0x0006)
        SET_LEAD_TIME = np.uint16(0x0007)
        SET_OCD_PARAMS = np.uint16(0x0008)
        START_TIMED_BURST_SEQUENCE = np.uint16(0x0009)
        GET_RES_PER_CAP_BUFFER = np.uint16(0x000A)

    class SolicitedDataId(Enum):
        RES_PER_CAP_CHUNK = np.uint16(1)

    class CommandResponseId(Enum):
        SUCCESS = np.uint16(0x0000)
        BUS_ERROR = np.uint16(0x0001)
        FAILURE = np.uint16(0xFFFF)

    class CommandResponseFlags(object):

        _ocd_mask : np.uint32 = 0x00000001
        _uvlo_mask : np.uint32 = 0x00000002
        _feedback_timeout_mask : np.uint32 = 0x00000004

        OCD_FAULT : bool = False
        UVLO_FAULT : bool = False
        FEEDBACK_TIMEOUT_FAULT : bool = False
        IsFaultActive : bool = False
        def __init__(self, flagMask: np.uint32):
            self._flagMask = flagMask
            self.IsFaultActive = self._flagMask != 0
            if self._flagMask & self._ocd_mask:
                self.OCD_FAULT = True
            else:
                self.OCD_FAULT = False

            if self._flagMask & self._uvlo_mask:
                self.UVLO_FAULT = True
            else:
                self.UVLO_FAULT = False

            if self._flagMask & self._feedback_timeout_mask:
                self.FEEDBACK_TIMEOUT_FAULT = True
            else:
                self.FEEDBACK_TIMEOUT_FAULT = False

    class SetOCDParamsCommandArgs(object):
        class CompHysteresis(Enum):
            NO_HYSTERESIS = np.uint16(0x0000)
            HYSTERESIS_10MV = np.uint16(0x0001)
            HYSTERESIS_20MV = np.uint16(0x0002)
            HYSTERESIS_30MV = np.uint16(0x0003)
            HYSTERESIS_40MV = np.uint16(0x0004)
            HYSTERESIS_50MV = np.uint16(0x0005)
            HYSTERESIS_60MV = np.uint16(0x0006)
            HYSTERESIS_70MV = np.uint16(0x0007)

        def __init__(self, ocdMaxCurrent_A: np.uint16, compHysteresis : CompHysteresis):
            self.OCDMaxCurrent_A : np.uint16 = ocdMaxCurrent_A
            self.InputHysteresis = compHysteresis

        def to_bytes(self)->np.ndarray:
            return np.concatenate([MujoeSerialCommRpi.uint16_to_bytes(self.OCDMaxCurrent_A, endianness="little"),
                                   MujoeSerialCommRpi.uint16_to_bytes(self.InputHysteresis.value, endianness="little")])

    class StartTimedBurstSequenceCommandArgs(object):
        def __init__(self, startFrequency_Hz: np.uint32, prfPeriod_us: np.uint32, prfOntime_us: np.uint32, leadTime_ns: np.uint32 = 0, sequenceDuration_ms: np.uint32 = np.uint32(1000)):
            self.StartFrequency_Hz : np.uint32 = startFrequency_Hz
            self.PRF_period_us : np.uint32 = prfPeriod_us
            self.PRF_ontime_us : np.uint32 = prfOntime_us
            self.LeadTime_ns : np.uint32 = leadTime_ns
            # Set burst cycle to 0, will be always set to 0 on firmware side
            self._num_burst_cycles : np.uint32 = np.uint32(0)
            self.SequenceDuration_ms : np.uint32 = sequenceDuration_ms

        def to_bytes(self)->np.ndarray:
            return np.concatenate([MujoeSerialCommRpi.uint32_to_bytes(self.StartFrequency_Hz, endianness="little"),
                                   MujoeSerialCommRpi.uint32_to_bytes(self.PRF_period_us, endianness="little"),
                                   MujoeSerialCommRpi.uint32_to_bytes(self.PRF_ontime_us, endianness="little"),
                                   MujoeSerialCommRpi.uint32_to_bytes(self.LeadTime_ns, endianness="little"),
                                   MujoeSerialCommRpi.uint32_to_bytes(self._num_burst_cycles, endianness="little"),
                                   MujoeSerialCommRpi.uint32_to_bytes(self.SequenceDuration_ms, endianness="little")])

    class StartBurstCommandArgs(object):
        def __init__(
            self,
            startFrequency_Hz: np.uint32,
            prfPeriod_us: np.uint32,
            prfOntime_us: np.uint32,
            leadTime_ns: np.uint32 = 0,
            numBurstCycles: np.uint32 = np.uint32(1),
            # Match firmware startBurstMsg_t / mujoe_resdrv defaults (25% neg, 5% pos)
            startFreqMaxNegDev_percnt: np.uint8 = np.uint8(25),
            startFreqMaxPosDev_percnt: np.uint8 = np.uint8(5),
        ):
            self.StartFrequency_Hz : np.uint32 = startFrequency_Hz
            self.PRF_period_us : np.uint32 = prfPeriod_us
            self.PRF_ontime_us : np.uint32 = prfOntime_us
            self.LeadTime_ns : np.uint32 = leadTime_ns
            self.NumBurstCycles : np.uint32 = numBurstCycles
            self.StartFreqMaxNegDev_percnt : np.uint8 = startFreqMaxNegDev_percnt
            self.StartFreqMaxPosDev_percnt : np.uint8 = startFreqMaxPosDev_percnt

        def to_bytes(self)->np.ndarray:
            # ARM EABI sizeof(startBurstMsg_t) == 24: 5x uint32 + 2x uint8 + 2 pad bytes
            return np.concatenate([MujoeSerialCommRpi.uint32_to_bytes(self.StartFrequency_Hz, endianness="little"),
                                   MujoeSerialCommRpi.uint32_to_bytes(self.PRF_period_us, endianness="little"),
                                   MujoeSerialCommRpi.uint32_to_bytes(self.PRF_ontime_us, endianness="little"),
                                   MujoeSerialCommRpi.uint32_to_bytes(self.LeadTime_ns, endianness="little"),
                                   MujoeSerialCommRpi.uint32_to_bytes(self.NumBurstCycles, endianness="little"),
                                   np.array([
                                       np.uint8(self.StartFreqMaxNegDev_percnt),
                                       np.uint8(self.StartFreqMaxPosDev_percnt),
                                       np.uint8(0),
                                       np.uint8(0),
                                   ], dtype=np.uint8)])

    class MujoeSerialFrame( object):
        def __init__( self, frame_id: np.uint16, payload_length: np.uint16, payload: np.ndarray, crc: np.uint16 ):
            self.frame_id = frame_id
            self.payload_length = payload_length
            self.payload = payload
            self.crc = crc

    class CommandResponse( object):
        def __init__( self, data: np.ndarray ):
            idx = 0
            # Extract CommandId from payload
            self.CommandId = np.uint16(data[idx])
            idx += 1
            self.CommandId |= (np.uint16(data[idx])<< 8)
            self.CommandId = MujoeSerialCommRpi.CommandId(self.CommandId)
            idx += 1
            # Extract Command Response from payload
            self.CommandResponseId = np.uint16(data[idx])
            idx += 1
            self.CommandResponseId |= (np.uint16(data[idx])<< 8)
            self.CommandResponseId = MujoeSerialCommRpi.CommandResponseId(self.CommandResponseId)
            idx += 1
            # Extract Flags (32 bits) from payload
            self.Flags = np.uint32(data[idx])
            idx += 1
            self.Flags |= (np.uint32(data[idx])<< 8)
            idx += 1
            self.Flags |= (np.uint32(data[idx])<< 16)
            idx += 1
            self.Flags |= (np.uint32(data[idx])<< 24)
            idx += 1
            # Extract ParamLength from payload
            self.ParamLength = data[idx]
            idx += 1
            # Extract Parameters from payload
            self.Parameters = data[idx:idx + self.ParamLength]

    class SolicitedData(object):
        """Parsed mujoe_sc_sol_data_t payload (little-endian)."""

        def __init__(self, data: np.ndarray):
            idx = 0
            self.DataId = np.uint16(data[idx]) | (np.uint16(data[idx + 1]) << 8)
            idx += 2
            self.DataLen = np.uint16(data[idx]) | (np.uint16(data[idx + 1]) << 8)
            idx += 2
            self.ChunkIdx = np.uint16(data[idx]) | (np.uint16(data[idx + 1]) << 8)
            idx += 2
            self.ChunkLen = np.uint16(data[idx]) | (np.uint16(data[idx + 1]) << 8)
            idx += 2
            # Only the valid portion of the fixed-size chunk array is used
            chunk_len = int(self.ChunkLen)
            self.Chunk = np.array(data[idx : idx + chunk_len], dtype=np.uint8, copy=True)

    class SolicitedDataAssembler(object):
        """Collects ordered SOL chunks into one buffer for a single data_id transfer."""

        def __init__(self):
            self.reset()

        def reset(self):
            self.armed = False
            self.expected_data_id = None
            self.data_len = 0
            self.next_chunk_idx = 0
            self.buf = bytearray()
            self.complete = False
            self.failed = False
            self.fail_reason = None

        def arm(self, data_id: np.uint16):
            self.reset()
            self.armed = True
            self.expected_data_id = np.uint16(data_id)

        def disarm(self):
            self.armed = False

        def feed(self, sol: "MujoeSerialCommRpi.SolicitedData") -> bool:
            """Append one chunk. Returns False on protocol error."""
            if not self.armed or self.complete or self.failed:
                return False
            if np.uint16(sol.DataId) != self.expected_data_id:
                self.failed = True
                self.fail_reason = f"unexpected data_id {sol.DataId}"
                return False
            if int(sol.ChunkIdx) != self.next_chunk_idx:
                self.failed = True
                self.fail_reason = (
                    f"out-of-order chunk_idx {sol.ChunkIdx}, expected {self.next_chunk_idx}"
                )
                return False
            # First chunk establishes total transfer size
            if self.next_chunk_idx == 0:
                self.data_len = int(sol.DataLen)
                self.buf = bytearray()
            chunk_len = int(sol.ChunkLen)
            if chunk_len > len(sol.Chunk):
                self.failed = True
                self.fail_reason = "chunk_len exceeds chunk bytes"
                return False
            self.buf.extend(bytes(sol.Chunk[:chunk_len]))
            self.next_chunk_idx += 1
            if self.data_len > 0 and len(self.buf) >= self.data_len:
                # Truncate any overshoot; complete when total payload received
                if len(self.buf) > self.data_len:
                    del self.buf[self.data_len :]
                self.complete = True
            return True

        def to_uint32_array(self) -> np.ndarray:
            """Interpret assembled LE bytes as uint32_t words (STM32 native)."""
            if (len(self.buf) % 4) != 0:
                raise ValueError(
                    f"assembled length {len(self.buf)} is not a multiple of 4"
                )
            return np.frombuffer(bytes(self.buf), dtype="<u4").copy()


    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0, logger: Logger = None):
        self.Port = port
        self.Baudrate = baudrate
        self.Bytesize = 8
        self.Stopbits = serial.STOPBITS_ONE
        self.Timeout = timeout
        self.Logger = logger
        self.parser_state = self.ParserState.IDLE
        self.parser_idx = 0
        self.parser_buffer : List[np.uint8] = []
        # Armed only during multi-chunk SOL awaits (e.g. res period capture dump)
        self._sol_assembler = self.SolicitedDataAssembler()
        self._sol_await_cmd_rsp = None
        self.reset_parser()

    def open(self)->bool:
        try:
            self._ser = serial.Serial(
                port=self.Port,
                baudrate=self.Baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.Timeout,
            )
            if self._ser.is_open:
                self._ser.flush()
                self._ser.reset_input_buffer()
                self._ser.reset_output_buffer()
                self._logInfo(f"Serial port {self._ser.name} opened")
                # Initialize GPIOs
                if not self.initGpios():
                    self._logError(f"Failed to initialize GPIOs")
                    return False
                return True
            else:
                self._logError(f"Failed to open serial port {self.Port}")
                return False
        except serial.SerialException as e:
            self._logError(f"open:: Serial error: {e}")
            return False

    def close(self)->bool:
        try:
            # Close serial port
            self._ser.close()
            # Release GPIOs to default state
            GPIO.cleanup()
            return True
        except Exception as e:
            self._logError(f"close:: Exception: {e}")
            return False

    def initGpios(self)->bool:
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.DRSSTC_NRST_GPIO, GPIO.OUT, initial=GPIO.HIGH)
            return True
        except Exception as e:
            self._logError(f"initGpios:: Exception: {e}")
            return False
    
    def hardwareReset(self)->bool:
        try:
            # Assert NRST low for 100 ms
            GPIO.output(self.DRSSTC_NRST_GPIO, GPIO.LOW)
            time.sleep(100e-3) # 100 ms negative pulse
            # Deassert NRST
            GPIO.output(self.DRSSTC_NRST_GPIO, GPIO.HIGH)
            time.sleep(500e-3) # 500 ms wait for DRSSTC MB to stabilize
            return True
        except Exception as e:
            self._logError(f"hardwareReset:: Exception: {e}")
            return False

    def write(self, data: bytes) -> int:
        return self._ser.write(data)

    def read(self,numBytes: int) -> bytes:
        return self._ser.read(numBytes)


    def reset_parser(self):
        self.parser_state = self.ParserState.IDLE
        self.parser_idx = 0
        self.parser_buffer.clear()
        self.frame_id = None
        self.payload_length = None
        self.payload = None
        self.crc = None

    def process_who_am_i_response(self, data: np.ndarray) -> str:
        if len(data) == 6:
            fwMajor = data[0]
            fwMinor = data[1]
            hwMajor = data[2]
            hwMinor = data[3]
            scMajor = data[4]
            scMinor = data[5]
            return f"Firmware v{fwMajor}.{fwMinor}, Hardware v{hwMajor}.{hwMinor}, Serial Comm v{scMajor}.{scMinor}"
        else:
            return None

    def process_command_response(self, command_response: CommandResponse) -> bool:
        if command_response.CommandResponseId == self.CommandResponseId.SUCCESS:
            self._logInfo(f"Command '{command_response.CommandId}' successful")
            if self.CommandId.WHO_AM_I == command_response.CommandId:
                version_info = self.process_who_am_i_response(command_response.Parameters)
                if version_info is not None:
                    self._logInfo(f"{version_info}")
            return True
        else:
            self._logError(f"Command '{command_response.CommandId}' failed w/ response: '{command_response.CommandResponseId}'")
            return False

    def process_frame(self, frame: MujoeSerialFrame) -> bool:
        if frame.frame_id == self.FrameId.COMMAND_RESPONSE.value:
            command_response = self.CommandResponse(frame.payload)
            # During SOL await, FW may send FAILURE CMD_RSP if dump fails to queue
            if self._sol_assembler.armed:
                self._sol_await_cmd_rsp = command_response
                if command_response.CommandResponseId != self.CommandResponseId.SUCCESS:
                    self._sol_assembler.failed = True
                    self._sol_assembler.fail_reason = (
                        f"CMD_RSP {command_response.CommandResponseId}"
                    )
            return self.process_command_response(command_response)
        elif frame.frame_id == self.FrameId.SOLICITED.value:
            if not self._sol_assembler.armed:
                self._logInfo("Solicited frame received but assembler not armed; ignoring")
                return True
            sol = self.SolicitedData(frame.payload)
            if not self._sol_assembler.feed(sol):
                self._logError(
                    f"SolicitedDataAssembler feed failed: {self._sol_assembler.fail_reason}"
                )
                return False
            return True
        else:
            self._logError(f"Unknown frame ID: {frame.frame_id}")
            return False

    def feed_parser(self,data: np.ndarray) -> MujoeSerialFrame:
        frame = None
        for byte in data:
            # Await preamble
            if self.parser_state == self.ParserState.IDLE:
                if byte == self.preamble[self.parser_idx]:
                    self.parser_idx += 1
                    if self.parser_idx == len(self.preamble):
                        self.parser_state = self.ParserState.FRAME_ID
                        self.parser_idx = 0
                else:
                    self.parser_idx = 0
            # Parse frame ID
            elif self.parser_state == self.ParserState.FRAME_ID:
                self.parser_buffer.append(byte)
                # Rx LSB of frame ID
                if self.parser_idx == 0:
                    self.frame_id = np.uint16(byte)
                    self.parser_idx += 1
                # Rx MSB of frame ID
                elif self.parser_idx == 1:
                    self.frame_id |= (np.uint16(byte)<< 8)
                    self.parser_idx = 0
                    self.parser_state = self.ParserState.PAYLOAD_LENGTH
            # Parse payload length
            elif self.parser_state == self.ParserState.PAYLOAD_LENGTH:
                self.parser_buffer.append(byte)
                # Rx LSB of payload length
                if self.parser_idx == 0:
                    self.payload_length = np.uint16(byte)
                    self.parser_idx += 1
                # Rx MSB of payload length
                elif self.parser_idx == 1:
                    self.payload_length |= (np.uint16(byte)<< 8)
                    self.payload = np.zeros(self.payload_length, dtype=np.uint8)
                    self.parser_idx = 0
                    self.parser_state = self.ParserState.PAYLOAD
            # Parse payload
            elif self.parser_state == self.ParserState.PAYLOAD:
                self.parser_buffer.append(byte)
                # Rx payload
                if self.parser_idx < self.payload_length:
                    self.payload[self.parser_idx] = byte
                    self.parser_idx += 1
                    # All payload rxd, go to CRC state
                    if self.parser_idx == self.payload_length:
                        self.parser_state = self.ParserState.CRC
                        self.parser_idx = 0
            # Parse CRC
            elif self.parser_state == self.ParserState.CRC:
                # Rx LSB of CRC
                if self.parser_idx == 0:
                    self.crc = np.uint16(byte)
                    self.parser_idx += 1
                # Rx MSB of CRC
                elif self.parser_idx == 1:
                    self.crc |= (np.uint16(byte)<< 8)
                    # Compute CRC and check if it is valid
                    computed_crc = calculate_crc16_ccitt(bytes(self.parser_buffer))
                    if self.crc == computed_crc:
                        self._logInfo(f"CRC is valid")
                        frame = self.MujoeSerialFrame(frame_id=self.frame_id,
                                                    payload_length=self.payload_length,
                                                    payload=self.payload,
                                                    crc=self.crc)
                        # Process valid frame
                        self.process_frame(frame)
                    else:
                        self._logError(f"CRC is invalid")
                    self.reset_parser()
        return frame

    def build_frame(self, frame_id : np.uint16, payload : np.ndarray) -> np.ndarray:
        # endianness: little endian
        frame = np.concatenate([self.preamble,
                                np.array([frame_id & 0xFF, frame_id >> 8], dtype=np.uint8),
                                np.array([len(payload) & 0xFF, len(payload) >> 8], dtype=np.uint8),
                                np.array(payload, dtype=np.uint8)])

        # Compute CRC excluding preamble
        crc = calculate_crc16_ccitt(bytes(frame[4:]))
        # Add CRC to frame
        frame = np.concatenate([frame, np.array([crc & 0xFF, crc >> 8], dtype=np.uint8)])
        return frame

    def build_command_frame(self, command_id : np.uint16, cmdArgs : np.ndarray) -> np.ndarray:


        if cmdArgs is None:
            payload = np.concatenate([np.array([command_id & 0xFF, command_id >> 8], dtype=np.uint8),
                            np.array([0], dtype=np.uint8)])
        else:
            if len(cmdArgs) > self.cmd_args_length:
                self._logError(f"Command arguments length is too long: {len(cmdArgs)}")
                return None

            # Pad command arguments with zeros to the length of cmd_args_length
            cmd_args = np.zeros(self.cmd_args_length, dtype=np.uint8)
            cmd_args[:len(cmdArgs)] = cmdArgs
            # Concatenate command id, command arguments length, and command arguments
            payload = np.concatenate([np.array([command_id & 0xFF, command_id >> 8], dtype=np.uint8),
                        np.array([len(cmdArgs) & 0xFF], dtype=np.uint8),
                        cmd_args])#np.array(cmd_args, dtype=np.uint8)])

        return self.build_frame(frame_id=self.FrameId.COMMAND.value, payload=payload)

    def await_command_response(self)->CommandResponse:
        cmdRsp = None
        rsp  = np.frombuffer(self.read(numBytes=self.cmd_rsp_frame_length), dtype=np.uint8)
        self.reset_parser();
        frame = self.feed_parser(rsp)
        if frame is not None:
            if frame.frame_id == self.FrameId.COMMAND_RESPONSE.value:
                cmdRsp = self.CommandResponse(frame.payload)
                if cmdRsp is not None:
                    self.CmdRspFlags = MujoeSerialCommRpi.CommandResponseFlags(flagMask=cmdRsp.Flags)

        return cmdRsp

    def perform_system_reset_await_response(self)->bool:
        ret = False
        cmdFrame = self.build_command_frame(command_id=self.CommandId.RESET.value, cmdArgs=None)
        self.write(bytes(cmdFrame))
        cmdRsp = self.await_command_response()
        if cmdRsp is not None:
            if cmdRsp.CommandId == self.CommandId.RESET:
                if cmdRsp.CommandResponseId == self.CommandResponseId.SUCCESS:
                    ret = True
        return ret

    # Start burst
    def start_burst_await_response(self, startBurstCommandArgs: StartBurstCommandArgs)->bool:
        ret = False
        cmdFrame = self.build_command_frame(command_id=self.CommandId.START_BURST.value, cmdArgs=startBurstCommandArgs.to_bytes())
        self.write(bytes(cmdFrame))
        cmdRsp = self.await_command_response()
        if cmdRsp is not None:
            if cmdRsp.CommandId == self.CommandId.START_BURST:
                if cmdRsp.CommandResponseId == self.CommandResponseId.SUCCESS:
                    ret = True
        return ret

    # Start timed burst sequence
    def start_timed_burst_sequence_await_response(self, startTimedBurstSequenceCommandArgs: StartTimedBurstSequenceCommandArgs)->bool:
        ret = False
        cmdFrame = self.build_command_frame(command_id=self.CommandId.START_TIMED_BURST_SEQUENCE.value, cmdArgs=startTimedBurstSequenceCommandArgs.to_bytes())
        self.write(bytes(cmdFrame))
        cmdRsp = self.await_command_response()
        if cmdRsp is not None:
            if cmdRsp.CommandId == self.CommandId.START_TIMED_BURST_SEQUENCE:
                if cmdRsp.CommandResponseId == self.CommandResponseId.SUCCESS:
                    ret = True
        return ret

    # Set OCD parameters
    def set_ocd_params_await_response(self, setOCDParamsCommandArgs: SetOCDParamsCommandArgs)->bool:
        '''Sets the Over Current Detection (OCD) parameters.
        Args:
            setOCDParamsCommandArgs: SetOCDParamsCommandArgs
        Returns:
            bool: True if successful, False otherwise
        '''
        ret = False
        cmdFrame = self.build_command_frame(command_id=self.CommandId.SET_OCD_PARAMS.value, cmdArgs=setOCDParamsCommandArgs.to_bytes())
        self.write(bytes(cmdFrame))
        cmdRsp = self.await_command_response()
        if cmdRsp is not None:
            if cmdRsp.CommandId == self.CommandId.SET_OCD_PARAMS:
                if cmdRsp.CommandResponseId == self.CommandResponseId.SUCCESS:
                    ret = True
        return ret
    
    # Set lead time in nanoseconds
    def set_lead_time_await_response(self, leadTime_ns: np.uint32)->bool:
        ret = False
        cmdFrame = self.build_command_frame(command_id=self.CommandId.SET_LEAD_TIME.value, cmdArgs=MujoeSerialCommRpi.uint32_to_bytes(leadTime_ns, endianness="little"))
        self.write(bytes(cmdFrame))
        cmdRsp = self.await_command_response()
        if cmdRsp is not None:
            if cmdRsp.CommandId == self.CommandId.SET_LEAD_TIME:
                if cmdRsp.CommandResponseId == self.CommandResponseId.SUCCESS:
                    ret = True
        return ret

    # Stop burst
    def stop_burst_await_response(self)->bool:
        ret = False
        cmdFrame = self.build_command_frame(command_id=self.CommandId.STOP_BURST.value, cmdArgs=None)
        self.write(bytes(cmdFrame))
        cmdRsp = self.await_command_response()
        if cmdRsp is not None:
            if cmdRsp.CommandId == self.CommandId.STOP_BURST:
                if cmdRsp.CommandResponseId == self.CommandResponseId.SUCCESS:
                    ret = True
        return ret

    def ctrl_hv_bus_await_response(self, powerOn: bool)->bool:
        ret = False
        cmdFrame = self.build_command_frame(command_id=self.CommandId.CTRL_HV_BUS.value, cmdArgs=np.array([powerOn], dtype=np.uint8))
        self.write(bytes(cmdFrame))
        cmdRsp = self.await_command_response()
        if cmdRsp is not None:
            if cmdRsp.CommandId == self.CommandId.CTRL_HV_BUS:
                if cmdRsp.CommandResponseId == self.CommandResponseId.SUCCESS:
                    ret = True
        return ret

    def ack_fault_await_response(self)->bool:
        ret = False
        cmdFrame = self.build_command_frame(command_id=self.CommandId.ACK_FAULT.value, cmdArgs=None)
        self.write(bytes(cmdFrame))
        cmdRsp = self.await_command_response()
        if cmdRsp is not None:
            if cmdRsp.CommandId == self.CommandId.ACK_FAULT:
                if cmdRsp.CommandResponseId == self.CommandResponseId.SUCCESS:
                    ret = True
        return ret

    def get_res_per_cap_buffer_await_response(self, progress_cb=None):
        """Send GET_RES_PER_CAP_BUFFER and assemble SOL chunks into a uint32 array.

        On success FW suppresses CMD_RSP and streams SC_FRAME_ID_SOL chunks.
        Returns np.ndarray dtype uint32 on success, None on failure/timeout.
        progress_cb(received_bytes, total_bytes) is optional; throttled so the
        UART loop is not slowed by downstream IPC.
        """
        # Throttle progress reports; the dump itself must not wait on the portal
        progress_period_s = 0.15
        last_progress_t = 0.0
        last_reported_len = -1
        result = None
        self.reset_parser()
        self._sol_await_cmd_rsp = None
        self._sol_assembler.arm(self.SolicitedDataId.RES_PER_CAP_CHUNK.value)

        cmdFrame = self.build_command_frame(
            command_id=self.CommandId.GET_RES_PER_CAP_BUFFER.value, cmdArgs=None
        )
        self.write(bytes(cmdFrame))

        # FW may delay ~1s before first SOL chunk; use serial Timeout as overall deadline
        deadline = time.time() + float(self.Timeout)
        try:
            while time.time() < deadline:
                if self._sol_assembler.complete:
                    break
                if self._sol_assembler.failed:
                    break
                # Drain whatever is waiting so 58-byte SOL and 62-byte CMD_RSP both parse
                waiting = 0
                try:
                    waiting = self._ser.in_waiting
                except Exception:
                    waiting = 0
                raw = self.read(numBytes=max(1, waiting))
                if not raw:
                    continue
                self.feed_parser(np.frombuffer(raw, dtype=np.uint8))

                if progress_cb is not None:
                    received = len(self._sol_assembler.buf)
                    now = time.time()
                    if received != last_reported_len and (
                        self._sol_assembler.complete
                        or now - last_progress_t >= progress_period_s
                    ):
                        last_progress_t = now
                        last_reported_len = received
                        try:
                            progress_cb(received, int(self._sol_assembler.data_len))
                        except Exception as e:
                            # A broken observer must not abort the transfer
                            self._logError(f"res per cap progress callback failed: {e}")

            if self._sol_assembler.complete:
                try:
                    result = self._sol_assembler.to_uint32_array()
                    self._logInfo(
                        f"Res period capture buffer assembled: {len(result)} words"
                    )
                except ValueError as e:
                    self._logError(f"get_res_per_cap_buffer_await_response: {e}")
                    result = None
            else:
                reason = self._sol_assembler.fail_reason or "timeout/incomplete"
                self._logError(
                    f"get_res_per_cap_buffer_await_response failed: {reason}"
                )
        finally:
            self._sol_assembler.disarm()
            self._sol_await_cmd_rsp = None

        return result

    def _logError(self,error:str):
        if self.Logger is not None:
            self.Logger.logError(f"MujoeSerialCommRpi: {error}")

    def _logInfo(self,info:str):
        if self.Logger is not None:
            self.Logger.logInfo(f"MujoeSerialCommRpi: {info}")


    @staticmethod
    def uint32_to_bytes(value: np.uint32, endianness: str = "little") -> np.ndarray:
        if endianness == "little":
            return np.array([(value & 0xFF), ((value >> 8) & 0xFF), ((value >> 16) & 0xFF), ((value >> 24) & 0xFF)], dtype=np.uint8)
        elif endianness == "big":
            return np.array([((value >> 24) & 0xFF), ((value >> 16) & 0xFF), ((value >> 8) & 0xFF), (value & 0xFF)], dtype=np.uint8)
        else:
            raise ValueError(f"Invalid endianness: {endianness}")
        
    @staticmethod
    def uint16_to_bytes(value: np.uint16, endianness: str = "little") -> np.ndarray:
        if endianness == "little":
            return np.array([(value & 0xFF), ((value >> 8) & 0xFF)], dtype=np.uint8)
        elif endianness == "big":
            return np.array([((value >> 8) & 0xFF), (value & 0xFF)], dtype=np.uint8)
        else:
            raise ValueError(f"Invalid endianness: {endianness}")


def main():
    # Default: RPi5 UART; override with MUJOE_SERIAL_PORT env or pass device path
    port = os.environ.get("MUJOE_SERIAL_PORT") or RPI_SERIAL_PORTS["DRSSTC_RPI4"]
    if len(sys.argv) >= 2:
        port = sys.argv[1]
    baudrate = 115200
    timeout = 5.0
    doSoftwareReset = False
    doHardwareReset = True
    logger = TerminalLogger( enableTimestamp=True)
    comm = MujoeSerialCommRpi(port=port,
                            baudrate=baudrate,
                            timeout=timeout,
                            logger=logger)
    if not comm.open():
        logger.logError(f"Failed to open serial port {port}")
        return
    
    # Perform hardware reset of DRSSTC MB
    if doHardwareReset:
        logger.logInfo(f"Performing hardware reset of DRSSTC MB")
        if not comm.hardwareReset():
            logger.logError(f"Failed to perform hardware reset of DRSSTC MB")
            return

    cmdFrame = comm.build_command_frame(command_id=MujoeSerialCommRpi.CommandId.WHO_AM_I.value, cmdArgs=None)

    comm.write(bytes(cmdFrame))
    rsp : bytes = comm.read(numBytes=62)
    rsp_array = np.frombuffer(rsp, dtype=np.uint8)
    comm.feed_parser(rsp_array)

    if doSoftwareReset:
        logger.logInfo(f"Performing software reset of DRSSTC MB")
        ret = comm.perform_system_reset_await_response()
        if ret:
            logger.logInfo(f"Software reset successful")
        else:
            logger.logError(f"Software reset failed")

    time.sleep(2)

    start_freq_hz = 264000 # 264 kHz
    prf_freq_hz = 200
    prf_period_us = 1/prf_freq_hz * 1e6
    prf_ontime_us = 100 # 5% DC of 200 Hz PRF
    lead_time_ns = 0

    startBurstCommandArgs = MujoeSerialCommRpi.StartBurstCommandArgs(
        startFrequency_Hz=start_freq_hz,
        prfPeriod_us=np.uint32(prf_period_us),
        prfOntime_us=np.uint32(prf_ontime_us),
        leadTime_ns=np.uint32(lead_time_ns),
        startFreqMaxNegDev_percnt=np.uint8(25),
        startFreqMaxPosDev_percnt=np.uint8(5),
    )
    ret = comm.start_burst_await_response(startBurstCommandArgs)
    if ret:
        logger.logInfo(f"Start burst successful")
    else:
        logger.logError(f"Start burst failed")
        logger.logInfo(f"OCD fault: {comm.CmdRspFlags.OCD_FAULT}")
        logger.logInfo(f"UVLO fault: {comm.CmdRspFlags.UVLO_FAULT}")
        logger.logInfo(f"Feedback timeout fault: {comm.CmdRspFlags.FEEDBACK_TIMEOUT_FAULT}")


if __name__ == "__main__":
    main()
