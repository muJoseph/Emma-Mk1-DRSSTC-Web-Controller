import sys
import os
# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enum import Enum
import time
from typing import Callable, Dict, List
from logging_interface.logger import Logger, TerminalLogger
from osal.osal_types import OsalError, OsalMessageUserMessage
from osal.osal_types import OsalMessage, OsalMessageSetEvent, OsalMessageClearEvent
from osal.osal_timer_process import OsalTimerProcess, OsalTimerInputMessage
from osal.osal_tools import timestamp
import multiprocessing as mp
from multiprocessing import Queue
from multiprocessing.context import SpawnContext
import queue            # used for empty queue exception 


# Keeps track of all events for the OSAL process
class OsalEvent( object ):
      EventId : int
      EventHandler : Callable
      IsPending : bool
      Delay_seconds : float
      AwaitingTimer : bool
      Periodic : bool

      def __init__(self, eventId : int, eventHandler):
            self.IsPending = False
            self.EventId = eventId
            self.EventHandler = eventHandler
            self.Delay_seconds = 0.0
            self.AwaitingTimer = False
            self.Periodic = False

class OsalHandle(object):
      OSALQueue : Queue
      EmitQueue : Queue
      _logger : Logger
      _osalProcessTargetArgs : object     # Lets a spawned handler reach this process instance through the handle

      def __init__(self, osalQueue : Queue, emitQueue : Queue, logger : Logger):
            self.OSALQueue = osalQueue
            self.EmitQueue = emitQueue
            self._logger = logger
            self._osalProcessTargetArgs = None

      def logInfo(self, inf : str):
            if self._logger is not None:
                  self._logger.logInfo(inf)
      
      def logError(self, err : str):
            if self._logger is not None:
                  self._logger.logError(err)

      def sendMessage(self, message : OsalMessage) -> OsalError:
            """Sends a message to the OSAL loop process"""
            if not self.OSALQueue.full():
                  self.OSALQueue.put(message)
                  return OsalError.OSAL_ERR_NONE
            else:
                  return OsalError.OSAL_ERR_FAILURE
      
      def setEvent(self, eventId : int, delay_seconds : float = 0.0, periodic : bool = False)->OsalError:
            """Sets an event to be triggered after a delay"""
            return self.sendMessage(OsalMessageSetEvent(eventId, delay_seconds, periodic))
      
      def clearEvent(self, eventId : int)->OsalError:
            """Clears an event"""
            return self.sendMessage(OsalMessageClearEvent(eventId))
      
      def emitMessage(self, message : object)->OsalError:
            """Emits a message to the OSAL process"""
            if self.EmitQueue.full():
                  return OsalError.OSAL_ERR_FAILURE
            else:
                  self.EmitQueue.put(message)
                  return OsalError.OSAL_ERR_NONE
      

# Arguments that will be passed to osalProcessTarget()
class OsalProcessTargetArgs(object):
      CurrentTime : float
      RxQueue : Queue
      EmitQueue : Queue
      Logger : TerminalLogger
      Name : str
      TickPeriod_sec : float
      Verbose : bool
      Events : Dict[int,OsalEvent]
      ClientId : int
      TimerRxQueue : Queue  # Add this to communicate with timer process
      OsalHdl : OsalHandle
      UserMessageHandler : Callable[[OsalHandle, OsalMessageUserMessage, object], None]

      def __init__(self, 
                   rxQueue : Queue,
                   emitQueue : Queue,
                   clientId : int, 
                   events : Dict[int,OsalEvent], 
                   logger : TerminalLogger, 
                   verbose : bool, name : str, 
                   tickPeriod_sec : float, 
                   timerRxQueue : Queue, 
                   eventHandlerArgs,
                   userMessageHandler : Callable[[OsalHandle, OsalMessageUserMessage], None]):
            # Initialize variables
            self.RxQueue = rxQueue
            self.EmitQueue = emitQueue
            self.Events = events
            self.Logger = logger
            self.Name = name
            self.TickPeriod_sec = tickPeriod_sec
            self.Verbose = verbose
            # Store queue instead of process
            self.TimerRxQueue = timerRxQueue  
            self.ClientId = clientId
            self.EventHandlerArgs = eventHandlerArgs
            # Create osal handle that will be passed to event handlers
            self.OsalHdl = OsalHandle(self.RxQueue, self.EmitQueue, self.Logger)
            self.OsalHdl._osalProcessTargetArgs = self # Spawned handlers receive the handle, not this object
            self.UserMessageHandler = userMessageHandler

      def logInfo(self, inf : str):
            if self.Logger is not None:
                  self.Logger.logInfo(f"{self.Name}: {inf}")

      def logError(self, err : str):
            if self.Logger is not None:
                  self.Logger.logError(f"{self.Name}: {err}")

def handleUserMessageEvent(args : OsalProcessTargetArgs, msg : OsalMessageUserMessage):
      if args.UserMessageHandler is None:
            args.logInfo(f"User message handler not set")
      else:
            args.UserMessageHandler(args.OsalHdl, msg, args.EventHandlerArgs)

# Handles messages received by the OSAL process
def handleTargetProcessMessages(args : OsalProcessTargetArgs):
            messages : List[OsalMessage] = []
            # Get all messages from queue
            while True:
                  # If get() is non-blocking (block=False), must handle empty queue exception
                  try:
                        msg = args.RxQueue.get(block=False)
                        if msg is not None:
                              messages.append(msg)
                  except queue.Empty:
                        break

            # Messages stay in arrival order. Running set/clear-event messages
            # before user messages is not implemented.
            
            # Handle messages
            for msg in messages:
                  # Convert message to correct type
                  msg = OsalMessage.create_from_message(msg)
                  
                  # Handle SET_EVENT or SET_EVENT_TIMER message
                  if msg.MessageId == OsalMessage.MessageIds.SET_EVENT or msg.MessageId == OsalMessage.MessageIds.SET_EVENT_TIMER:
                        if msg.EventId in args.Events:
                              evt = args.Events[msg.EventId]
                              # Save periodic flag for event cancellation
                              if msg.MessageId == OsalMessage.MessageIds.SET_EVENT:
                                    evt.Periodic = msg.Periodic
                              # Start timer if delay is specified
                              if msg.Delay_seconds > 0.0:
                                    # Notify timer process of a delayed event
                                    args.TimerRxQueue.put(OsalTimerInputMessage(args.ClientId, msg.EventId, msg.Delay_seconds, msg.Periodic))
                                    evt.AwaitingTimer = True
                              # Set event immediately
                              else:
                                    evt.IsPending = True
                                    # Only clear awaiting timer flag if this is not a periodic event
                                    # since a periodic event will be rescheduled by the timer process
                                    if not evt.Periodic:
                                          evt.AwaitingTimer = False
                  # Handle CLEAR_EVENT message  
                  elif msg.MessageId == OsalMessage.MessageIds.CLEAR_EVENT:
                        if msg.EventId in args.Events:
                              evt = args.Events[msg.EventId]
                              # Cancel timer in osal timer process if this was a delayed event
                              if evt.AwaitingTimer:
                                    # Send cancel message through queue
                                    args.TimerRxQueue.put(OsalTimerInputMessage(args.ClientId, msg.EventId, -1.0))
                                    evt.AwaitingTimer = False
                                    evt.Periodic = False

                              # Clear pending flag
                              evt.IsPending = False
                  elif msg.MessageId == OsalMessage.MessageIds.USER_MESSAGE_EVENT:
                        handleUserMessageEvent(args, msg)
                  else:
                        args.logError(f"Unknown message type: {msg.MessageId}")

# Handles events that are pending within the OSAL process
def handleTargetEvents(args : OsalProcessTargetArgs):
      # If event is pending, call the event handler
      for eventId, evt in args.Events.items():
            if evt.IsPending:
                  if args.Verbose:
                        args.logInfo(f"Event handler for event {eventId} called")
                  evt.EventHandler(args.OsalHdl, args.EventHandlerArgs)
                  evt.IsPending = False

# Main loop for the OSAL process, passed to mp.Process() as the target function
def osalProcessTarget(args : OsalProcessTargetArgs):
            try:
                  while True:
                        # Handle messages
                        handleTargetProcessMessages(args)
                        # Handle events
                        handleTargetEvents(args)
                        # Sleep for 1 timer tick period     
                        time.sleep(args.TickPeriod_sec)
            except KeyboardInterrupt:
                  args.logInfo("Loop terminated, cleaning up...")
                  args.logInfo("Cleanup finished")


class OsalProcessConfig(object):
      TickPeriod_sec : float
      Name : str
      Verbose : bool
      RxQueueSize : int
      EmitQueueSize : int
      UserMessageHandler : Callable[[OsalHandle, OsalMessageUserMessage], None]

      class Presets(Enum):
            DEFAULT = 0

      def __init__(self, tickPeriod_sec : float, name : str, verbose : bool, rxQueueSize : int, emitQueueSize : int):
            self.TickPeriod_sec = tickPeriod_sec
            self.Name = name
            self.Verbose = verbose
            self.RxQueueSize = rxQueueSize
            self.EmitQueueSize = emitQueueSize
            self.UserMessageHandler = None
      @classmethod
      def create_from_preset(cls, preset : Presets)->'OsalProcessConfig':
            if preset == cls.Presets.DEFAULT:
                  return OsalProcessConfig(tickPeriod_sec=1e-3, name='osalProcess', verbose=False, rxQueueSize=20, emitQueueSize=20)
            else:
                  return None
      

class OsalProcess(object):
      Config : OsalProcessConfig
      # Queue for messages to be emitted by the OSAL process, can be consumed by other processes
      # and/or main loop
      EmitQueue : Queue

      def __init__(self, osalTimerProcess : OsalTimerProcess, config : OsalProcessConfig, logger : Logger = None ):
            self.Config = config
            self.__logger = logger
            self.__events : Dict[int,OsalEvent] = {}
            self.__rxQueue = Queue(maxsize=self.Config.RxQueueSize)
            self.EmitQueue = Queue(maxsize=self.Config.EmitQueueSize)
            self.__osalTimerProcess = osalTimerProcess
            # Register this osal process with the timer process
            self.__clientId, error = self.__osalTimerProcess.registerClient(self.__rxQueue)
            self._p = None
            self.__eventHandlerArgs = None

      def start(self, context : SpawnContext):
            # Create process
            self._p = context.Process(target=osalProcessTarget, 
                                      args=(OsalProcessTargetArgs(self.__rxQueue, 
                                                              self.EmitQueue,
                                                              self.__clientId, 
                                                              self.__events, 
                                                              self.__logger, 
                                                              self.Config.Verbose, 
                                                              self.Config.Name, 
                                                              self.Config.TickPeriod_sec,
                                                              self.__osalTimerProcess.RxQueue,
                                                              self.__eventHandlerArgs,
                                                              self.Config.UserMessageHandler),))

            """Starts the OSAL loop process"""
            self.__logInfo("Starting process...")
            self._p.start()
            # Wait for process to start
            while not self._p.is_alive():
                  time.sleep(1e-3)
            self.__logInfo("Process started")
      


      @classmethod
      def handleOsalMessagesExtern(cls, args : OsalProcessTargetArgs ):
            """Use with caution"""
            handleTargetProcessMessages(args)

      def setEventHandlerArgs(self, eventHandlerArgs ):
            self.__eventHandlerArgs = eventHandlerArgs

      def sendMessage(self, message : OsalMessage) -> OsalError:
            """Sends a message to the OSAL loop process"""
            if not self.__rxQueue.full():
                  self.__rxQueue.put(message) 
                  if self.Config.Verbose:
                        self.__logInfo(f"sendMessage: msg id '{message.MessageId}' added to queue")
                  return OsalError.OSAL_ERR_NONE
            else:
                  self.__logError("sendMessage: Queue is full")
                  return OsalError.OSAL_ERR_FAILURE
      
      def sendUserMessage(self, userMessageId : int, data : object)->OsalError:
            """Sends a user message to the OSAL loop process"""
            return self.sendMessage(OsalMessageUserMessage(userMessageId, data))

      def setEvent(self, eventId : int, delay_seconds : float = 0.0, periodic : bool = False)->OsalError:
            """Sets an event to be triggered after a delay"""
            return self.sendMessage(OsalMessageSetEvent(eventId, delay_seconds, periodic))
      
      def clearEvent(self, eventId : int)->OsalError:
            """Clears an event"""
            return self.sendMessage(OsalMessageClearEvent(eventId))

      def registerEvent(self, eventId : int, eventHandler) -> bool:
            """Registers an event with the OSAL process. MUST be called before start()"""
            self.__events[eventId] = OsalEvent(eventId, eventHandler)
            return True

      def terminate(self):
            """Terminates the OSAL loop process"""
            self.__logInfo("Terminating process...")
            self._p.terminate()
            # Wait for process to terminate
            while self._p.is_alive():
                  time.sleep(1e-3)
            self.__logInfo("Process terminated")

      def __logError(self,err:str):
            if self.__logger is not None:
                  self.__logger.logError(f"{self.Config.Name}: {err}")

      def __logInfo(self,inf:str):
            if self.__logger is not None:
                  self.__logger.logInfo(f"{self.Config.Name}: {inf}")

################################################################
# Testing Variables
################################################################

class EventHandlerArgs(object):
      SharedData : Dict[str,str]
      Counter : int
      def __init__(self, sharedData : Dict[str,str], counter : int):
            self.SharedData = sharedData
            self.Counter = counter

# NOTE: Event handlers must at module scope, not local scope, in order
# for python to pickle them (serialize them) when the process is spawned
def eventHandler1(osalHdl : OsalHandle, args : EventHandlerArgs):
      print("eventHandler1 called")
      if args is not None:
            args.Counter += 1
            print(f"SharedData: {args.SharedData['SomeData']}")

def eventHandler2(osalHdl : OsalHandle, args : EventHandlerArgs):
      print("eventHandler2 called")
      if args is not None:
            print(f"Counter: {args.Counter}")
            if args.SharedData["SomeData"] == "Hello":
                  args.SharedData["SomeData"] = "Goodbye"
            else:
                  args.SharedData["SomeData"] = "Hello"
      
################################################################
# Main
################################################################
if __name__ == "__main__":
      def main():
            """Main function"""
            class EventIds( Enum ):
                  EVENT_1 = 1
                  EVENT_2 = 2

            eventHdlrArgs = EventHandlerArgs(sharedData={"SomeData": "Hello"}, counter=0)

            mp.freeze_support()
            # Set the spawn method for the process
            mp.set_start_method(method="spawn")

            # Create logger
            l = TerminalLogger()
            # Create timer process
            osalTimerProcess = OsalTimerProcess(logger=l, verbose=True, name="osalTimer")

            # Create OSAL process
            osalConfig = OsalProcessConfig.create_from_preset(OsalProcessConfig.Presets.DEFAULT)
            osal = OsalProcess(osalTimerProcess=osalTimerProcess, config=osalConfig, logger=l) 
            # Set event handler args
            osal.setEventHandlerArgs(eventHdlrArgs)

            # Register events
            osal.registerEvent(EventIds.EVENT_1.value, eventHandler1)
            osal.registerEvent(EventIds.EVENT_2.value, eventHandler2)

            # Spawn start method so child processes do not inherit the parent's threads
            ctx = mp.get_context("spawn")

            # Start timer process
            osalTimerProcess.start(context=ctx)

            # Start OSAL process
            osal.start(context=ctx)

            # Start a periodic event
            osal.setEvent(EventIds.EVENT_2.value, delay_seconds=5.0, periodic=True)
            
            mainLoopTickPeriod_sec = 1.0  # 1 second
            elapsedTime_sec = 0.0
            periodicEventEnabled = True

            # Terminal main loop
            mainTick = 0
            try:
                  while True:
                        mainTick += 1
                        print(f"Main loop tick #{mainTick}")
                        # Set a one-time event immediately
                        osal.setEvent(EventIds.EVENT_1.value)
                        time.sleep(mainLoopTickPeriod_sec)
                        elapsedTime_sec += mainLoopTickPeriod_sec
                        # Disable periodic event after 20 seconds
                        if elapsedTime_sec > 20.0 and periodicEventEnabled:
                              print("Disabling periodic event")
                              osal.clearEvent(EventIds.EVENT_2.value)
                              periodicEventEnabled = False
            # On keyboard interrupt (Ctrl+C)
            except KeyboardInterrupt:
                  osal.terminate()
                  osalTimerProcess.terminate()
                  print("User terminated program, exiting...")

      print("running main...")
      main()


