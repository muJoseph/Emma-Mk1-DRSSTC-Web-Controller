import sys
import os

# Add the parent directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import time
from typing import List

class Logger( object ):
    LogBuffer : List[str]
    LogLevel : str
    LogFileName : str
    LogBatchSize : int

    def __init__(self, errorHeader : str = "@ERR::", infoHeader : str = "@INFO::", enableTimestamp : bool = True):
        self.errHeader = errorHeader
        self.infHeader = infoHeader
        self.EnableTimestamp = enableTimestamp
        self.EnableDate = False
        self.LogFileName = None
        self.LogLevel = "none"

    def enableLogFile( self, fileName : str, logLevel : str = "all", logBatchSize : int = 100 ):
        self.LogFileName = fileName
        self.LogLevel = logLevel
        self.LogBuffer : List[str] = []
        self.LogBatchSize = logBatchSize
    
    def _writeToLogFile( self ):
        if self.LogFileName is not None:
            # Open file in append mode
            with open(self.LogFileName,'a') as f:
                for l in self.LogBuffer:
                    f.write(f"{l}\n")
            self.LogBuffer.clear()

    def _appendLog(self, log : str ):
        if self.LogFileName is not None:
            self.LogBuffer.append(log)
            if len(self.LogBuffer) == self.LogBatchSize:
                self._writeToLogFile()
    
    def appendLogInfo(self, info : str):
        if self.LogLevel == "info" or self.LogLevel == "all":
            self._appendLog(info)
    
    def appendLogError(self, error : str):
        if self.LogLevel == "error" or self.LogLevel == "all":
            self._appendLog(error)

    def dumpLogsToFile(self):
        self._writeToLogFile()


    def getTimeStamp( self )->str:
        dt = datetime.datetime.fromtimestamp(time.time())
        if self.EnableDate:
            return dt.strftime("%Y-%m-%d %H:%M:%S.%f") # Date, time, and microseconds
        else:
            return dt.strftime("%H:%M:%S.%f")   # Hour, minute, second, milliseconds

    def logError(self, err):
        self.appendLogError(err)

    def logInfo(self, info):
        self.appendLogInfo(info)


class TerminalLogger(Logger):
    def __init__(self, errorHeader = "@ERR::", infoHeader = "@INFO::", enableTimestamp = True):
        super().__init__(errorHeader, infoHeader, enableTimestamp)

    def logError(self, err):
        error = self.errHeader
        if self.EnableTimestamp:
            error += self.getTimeStamp() + "::"
        error += " " + err
        print(error)
        super().logError(error)

    def logInfo(self, info):
        inf = self.infHeader
        if self.EnableTimestamp:
            inf += self.getTimeStamp() + "::"
        inf += " " + info
        print(inf)
        super().logInfo(inf)

    def deInit(self):
        self.dumpLogsToFile()

################################################################
# Testing
################################################################
def main():
    dt = datetime.datetime.fromtimestamp(time.time())
    dt_str = dt.strftime("%Y%m%d_%H%M")
    #dt_str = dt.strftime("%mm%dd%YY_%HH%MM")
    fn = f"test_log_{dt_str}.txt"
    l = TerminalLogger()
    l.enableLogFile(fn,logLevel="all", logBatchSize=10)

    for i in range(10):
        l.logError(f"This is error #{i}")
        l.logInfo(f"This is info #{i}")

    # Dump any messages to file
    l.deInit()
if __name__=="__main__":
    main()

