from logging import Logger
from typing import Dict, List, Tuple
import time
from logging_interface.logger import TerminalLogger
from osal.osal_types import OsalMessageSetEvent, OsalMessageSetEventTimer, OsalError

import multiprocessing as mp
from multiprocessing import Queue
from multiprocessing.context import SpawnContext
import queue
from enum import Enum

class OsalTimerInputMessage(object):
      def __init__(self, clientId : int, eventId : int, delay_sec : float = -1.0, periodic : bool = False):
            self.ClientId = clientId
            self.EventId = eventId
            # If delay_sec is -1.0, timer is cancelled
            self.Delay_seconds = delay_sec
            self.Periodic = periodic

class OsalTimer(object):
      def __init__(self, currentTime : float, eventId : int, delay_sec : float, periodic : bool = False):
            self.Delay_seconds = delay_sec
            self.TimerEndTime = currentTime + self.Delay_seconds
            self.EventId = eventId
            self.Periodic = periodic

      def updateTimeout(self, currentTime : float):
            if self.Periodic:
                  self.TimerEndTime = currentTime + self.Delay_seconds

class OsalTimerClient(object):
      def __init__(self, clientId : int,  clientRxQueue : Queue):
            self.ClientId = clientId
            self.ClientRxQueue = clientRxQueue
            self.OsalTimers : Dict[int,OsalTimer] = {}

      def registerTimer(self, currentTime : float, eventId : int, delay_sec : float, periodic : bool = False):
            self.OsalTimers[eventId] = OsalTimer(currentTime, eventId, delay_sec, periodic)
      
      def unregisterTimer(self, eventId : int)->OsalTimer:
            """Removes a timer from the client and returns it"""
            try:
                  # Remove timer from dict
                  return self.OsalTimers.pop(eventId)
            except KeyError:
                  return None

      def getTimer(self, eventId : int)->OsalTimer:
            if eventId in self.OsalTimers:
                  return self.OsalTimers[eventId]
            else:
                  return None
      
      def handleExpiredTimer(self, currentTime : float, eventId : int):
            """Handles an expired timer"""
            timer = self.getTimer(eventId)
            if timer is not None:
                  # Timer is periodic, update timeout for next iteration
                  if timer.Periodic:
                        timer.updateTimeout(currentTime)
                  else:
                        # Timer is not periodic, unregister it
                        self.unregisterTimer(eventId)
                  
                  # Notify client of expiration
                  self.notifyClient(eventId)

      def notifyClient(self, eventId : int):
            self.ClientRxQueue.put(OsalMessageSetEventTimer(eventId))

      def areTimersPending(self)->bool:
            return len(self.OsalTimers) > 0


class OsalTimerProcessTargetArgs(object):
      CurrentTime : float
      RxQueue : Queue
      QueueGetBlocking : bool
      Clients : Dict[int,OsalTimerClient]
      Logger : TerminalLogger
      Name : str
      TickPeriod_sec : float
      Verbose : bool

      def __init__(self, rxQueue : Queue, clients : Dict[int,OsalTimerClient], logger : TerminalLogger, verbose : bool, name : str, tickPeriod_sec : float):
            self.RxQueue = rxQueue
            self.QueueGetBlocking = True
            self.Clients = clients
            self.Logger = logger
            self.Name = name
            self.TickPeriod_sec = tickPeriod_sec
            self.Verbose = verbose

      def logInfo(self, inf : str):
            if self.Logger is not None:
                  self.Logger.logInfo(f"{self.Name}: {inf}")

      def logError(self, err : str):
            if self.Logger is not None:
                  self.Logger.logError(f"{self.Name}: {err}")

def handleTargetProcessMessages(args : OsalTimerProcessTargetArgs):
      messages : List[OsalTimerInputMessage] = []
      # Get all messages from queue
      while True:
            #msg : OsalTimerInputMessage = None
            # If get() is non-blocking (block=False), must handle empty queue exception
            try:
                  # Get message from queue
                  msg = args.RxQueue.get(block=args.QueueGetBlocking)
                  # If message is not None, add to list     
                  if msg is not None:
                        messages.append(msg)

                  # Do not block anymore on get(), will be set again later in code if there are timers pending
                  args.QueueGetBlocking = False
            except queue.Empty:
                  break

      # Handle messages
      for msg in messages:
            # Check if client is registered
            if msg.ClientId in args.Clients:
                  # Cancel timer
                  if msg.Delay_seconds == -1.0:
                        try:
                              # Unregister timer from client
                              # Discard timer instance since we we cancelled it
                              args.Clients[msg.ClientId].unregisterTimer(msg.EventId)
                        except KeyError:
                              args.logError(f"Timer {msg.EventId} not found for client {msg.ClientId}")
                  # Register timer
                  else:
                        args.Clients[msg.ClientId].registerTimer(args.CurrentTime, msg.EventId, msg.Delay_seconds, msg.Periodic)
            else:
                  args.logError(f"Client {msg.ClientId} not found!")

def osalTimerProcessTargetCheckTimers(args : OsalTimerProcessTargetArgs):
            expiredTimerIds : List[(int,int)] = []

            # Find expired timers in all clients
            for clientId, client in args.Clients.items():
                  for eventId, timer in client.OsalTimers.items():
                        # Check if timer has expired
                        if timer.TimerEndTime <= args.CurrentTime:
                              # Timer has expired, add to list of expired timers
                              expiredTimerIds.append((clientId,eventId))

            # Go through expired timers and handle them
            for clientId, eventId in expiredTimerIds:
                  args.Clients[clientId].handleExpiredTimer(args.CurrentTime, eventId)
                  if args.Verbose:
                        args.logInfo(f"Timer {eventId} for client {clientId} has expired")

            # If NO timers across any clients are pending, cause a blocking get() on queue
            # so that this process is not needlessly spinning
            args.QueueGetBlocking = True
            for client in args.Clients.values():
                  if client.areTimersPending():
                        args.QueueGetBlocking = False
                        break;

def osalTimerProcessTarget(args : OsalTimerProcessTargetArgs):
            try:
                  while True:
                        # Get current time of current loop iteration
                        args.CurrentTime = time.time()

                        # Handle messages from clients
                        handleTargetProcessMessages(args)

                        # Check timers
                        osalTimerProcessTargetCheckTimers(args)

                        # Sleep for 1 timer tick period     
                        time.sleep(args.TickPeriod_sec)
            # SIGINT is sent to process when terminate() is called
            except KeyboardInterrupt:
                  args.logInfo("Loop terminated, cleaning up...")
                  # TODO: Do cleanup
                  args.logInfo("Cleanup finished")

class OsalTimerProcess(object):
      # OSAL timer tick period in seconds
      OSAL_LOOP_TIMER_TICK_PERIOD_SEC = 500e-6   # 500us

      def __init__(self, tickPeriod_sec : float = OSAL_LOOP_TIMER_TICK_PERIOD_SEC, name : str = "OsalTimerProcess", logger : Logger = None, verbose : bool = False):
            self.__logger = logger
            self.__verbose = verbose
            self.__name = name
            self.TickPeriod_sec = tickPeriod_sec
            self.TickFrequency_Hz = 1.0 / self.TickPeriod_sec
            self.__queueGetBlocking = True
            # List of clients using the timer process
            self.__clients : Dict[int,OsalTimerClient] = {}
            self.RxQueue = Queue(maxsize=10)
            self._p = None
            
      # Methods
      def registerClient(self, clientRxQueue : Queue) -> Tuple[int, OsalError]:
            # Ensure internal process is NOT running
            if self._p is None:
                  clientId = len(self.__clients) + 1
                  self.__clients[clientId] = OsalTimerClient(clientId, clientRxQueue)
                  return clientId, OsalError.OSAL_ERR_NONE
            else:
                  return -1, OsalError.OSAL_ERR_FAILURE

      def startTimer(self, clientId : int, eventId : int, delay_sec : float, periodic : bool = False):
            self.RxQueue.put(OsalTimerInputMessage(clientId, eventId, delay_sec, periodic))

      def cancelTimer(self, clientId : int, eventId : int):
            self.startTimer(clientId, eventId, -1.0)

      def start(self, context : SpawnContext):
            # Create process
            self._p = context.Process(target=osalTimerProcessTarget, 
                                      args=( OsalTimerProcessTargetArgs(self.RxQueue, 
                                                                        self.__clients, 
                                                                        self.__logger, 
                                                                        self.__verbose, 
                                                                        self.__name, 
                                                                        self.TickPeriod_sec), ) )

            """Starts the OSALTimer loop process"""
            self.__logInfo("Starting process...")
            self._p.start()
            # Wait for process to start
            while not self._p.is_alive():
                  time.sleep(1e-3)
            self.__logInfo("Process started")

      def terminate(self):
            """Terminates the OSALTimer loop process"""
            if self._p is not None:
                  self.__logInfo("Terminating process...")
                  self._p.terminate()
                  # Wait for process to terminate
                  while self._p.is_alive():
                        time.sleep(1e-3)
                  self.__logInfo("Process terminated")

      def __logInfo(self, inf : str):
            if self.__logger is not None:
                  self.__logger.logInfo(f"{self.__name}: {inf}")

      def __logError(self, err : str):
            if self.__logger is not None:
                  self.__logger.logError(f"{self.__name}: {err}")


################################################################
# Testing
################################################################
if __name__ == "__main__":
      def main():
            # Set the spawn method for the process
            mp.set_start_method(method="spawn")

            l = TerminalLogger()
            osalTimerProcess = OsalTimerProcess(logger=l, verbose=True, name="OsalTimerProcess")

            # Create queues for clients
            myQueue1 = Queue(maxsize=10)
            myQueue2 = Queue(maxsize=10)

            # Register clients, this will fail if process is already running
            clientId1, err1 = osalTimerProcess.registerClient(clientRxQueue=myQueue1)
            clientId2, err2 = osalTimerProcess.registerClient(clientRxQueue=myQueue2)

            if err1 != OsalError.OSAL_ERR_NONE:
                  print(f"Error registering client 1: {err1}")
            if err2 != OsalError.OSAL_ERR_NONE:
                  print(f"Error registering client 2: {err2}")

            # Spawn start method so child processes do not inherit the parent's threads
            ctx = mp.get_context("spawn")

            # Start osal timer process
            osalTimerProcess.start(context=ctx)

            # Register timers with client1
            osalTimerProcess.startTimer(clientId=clientId1, eventId=1, delay_sec=2.5)
            osalTimerProcess.startTimer(clientId=clientId1, eventId=2, delay_sec=5.0)
            osalTimerProcess.startTimer(clientId=clientId1, eventId=3, delay_sec=500e-3)

            # Register/cancel timers with clients 1 and 2 after delay
            time.sleep(1.0)
            osalTimerProcess.cancelTimer(clientId=clientId1, eventId=2)
            osalTimerProcess.startTimer(clientId=clientId2, eventId=4, delay_sec=4.0)
            # Start a periodic timer
            osalTimerProcess.startTimer(clientId=clientId2, eventId=5, delay_sec=2.0, periodic=True)

            mainLoopTickPeriod_sec = 500e-6
            elapsedTime_sec = 0.0
            periodicEventCancelled = False

            # Main terminal loop
            # Wait for messages from OSAL timer process
            print("Starting main loop")
            while True:
                  msg : OsalMessageSetEvent = None
                  try:
                        # Perform non-blocking message get from client 1 queue      
                        try:
                              msg = myQueue1.get(block=False)
                              if msg is not None:
                                    print(f"Client 1 event {msg.EventId} message received")
                        except queue.Empty:
                              pass

                        # Perform non-blocking message get from client 2 queue      
                        try:
                              msg = myQueue2.get(block=False)
                              if msg is not None:
                                    print(f"Client 2 event {msg.EventId} message received")
                        except queue.Empty:
                              pass  

                        # Cancel periodic timer after 10 seconds
                        if elapsedTime_sec >= 10.0 and not periodicEventCancelled:
                              print("Cancelling periodic timer")
                              # Cancel periodic timer
                              osalTimerProcess.cancelTimer(clientId=clientId2, eventId=5)
                              periodicEventCancelled = True


                        # Sleep for 500us
                        time.sleep(mainLoopTickPeriod_sec)
                        elapsedTime_sec += mainLoopTickPeriod_sec

                  except KeyboardInterrupt:
                        osalTimerProcess.terminate()
                        break;

      print("running main...")
      main()
            

